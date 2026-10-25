"""MCP routes: list permitted tools for a config (``/tools``), execute a tool call
with permission checks (``/call``), and reload the tool registry (internal)."""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from api.database import get_session
from api.devices.presence import online_tool_defs
from mcp_runtime.mcp import registry
from mcp_runtime.mcp.loader import reload_registry
from api.models import AssistantAIConfig
from .auth import get_current_user
from api.runtime.internal_http import require_internal_token
from api.services.mcp.mcp_tool_runner import run_inheritance_mcp_test
from ai_runtime.inference.tool_execution import call_mcp_or_endpoint_tool
from connector_runtime.dispatch.desktop_device_tools import (
    connected_endpoint_tool_catalog,
    endpoint_bridge_tools_for_config,
    endpoint_tools_for_config,
    is_endpoint_agent_tool,
    is_workshop_tool,
)
from api.services.mcp.mcp_prompt_groups import build_prompt_tool_groups

router = APIRouter()
PREFIX = "/api/mcp"


def _require_endpoint_ai_config(tool: str, ai_config_id: Optional[int]) -> None:
    """Require an AI context before routing a tool to a live endpoint.

    Server-owned tools can be called by unscoped admin/library flows. Device
    tools cannot: their target and permissions come from the AI's binding and
    MCP scope, so accepting a missing config would make the console path
    ambiguous and would produce a late connector failure.
    """
    if is_endpoint_agent_tool(tool) and ai_config_id is None:
        raise HTTPException(
            status_code=400,
            detail="ai_config_id is required for endpoint MCP tools",
        )


def _require_bound_inheritance_device(
    user_id: int,
    ai_config_id: Optional[int],
    device_id: str,
) -> None:
    if ai_config_id is None:
        return
    from api.devices.bindings import device_ids_for_config

    if str(device_id or "").strip() not in set(device_ids_for_config(user_id, ai_config_id)):
        raise HTTPException(status_code=403, detail="Device is not bound to this AI")


class MCPCallRequest(BaseModel):
    tool: str = Field(..., description="Fully qualified MCP tool name")
    arguments: Optional[Dict[str, Any]] = Field(default_factory=dict)
    ai_config_id: Optional[int] = None


class InheritanceMcpTestRequest(BaseModel):
    model_preset_id: str = Field(..., description="Server model preset id from user.model_presets")
    tool: str
    device_id: str
    ai_config_id: Optional[int] = None
    device_type: str = "desktop"
    description: str = ""
    parameters: Optional[List[Dict[str, Any]]] = None
    input_schema: Optional[Dict[str, Any]] = None
    implementation: Optional[Dict[str, Any]] = None
    user_hint: str = ""


class MCPJobCreateRequest(BaseModel):
    """Asynchronous wrapper for endpoint MCP calls.

    Server/workspace tools stay synchronous for compatibility.  Endpoint calls
    use the persisted ``AgentDispatchTask`` state machine so a reconnect or a
    connector restart does not lose the result.
    """

    tool: str
    arguments: Optional[Dict[str, Any]] = Field(default_factory=dict)
    ai_config_id: Optional[int] = None
    timeout_seconds: int = Field(default=120, ge=1, le=1800)


class MCPJobCancelRequest(BaseModel):
    reason: str = "cancelled by caller"


def _console_target(arguments: Optional[Dict[str, Any]]) -> str:
    return str((arguments or {}).get("target") or "workspace").strip().lower()


def _console_endpoint_tool(arguments: Optional[Dict[str, Any]]) -> str:
    from tools.console import console_device_request

    return console_device_request(arguments or {})[0]


def _dispatch_job_payload(row: Any) -> Dict[str, Any]:
    import json

    result: Any = None
    if row.result_json:
        try:
            result = json.loads(row.result_json)
        except (TypeError, ValueError):
            result = row.result_json
    return {
        "job_id": row.task_id,
        "status": row.status,
        "success": row.success,
        "summary": row.summary,
        "error": row.error,
        "result": result,
        "tool": row.tool,
        "device_id": row.device_id,
        "ai_config_id": row.ai_config_id,
        "created_at": row.created_at,
        "completed_at": row.completed_at,
    }


def _owned_dispatch_row(session: Session, user_id: int, task_id: str):
    from api.models import AgentDispatchTask

    row = session.exec(
        select(AgentDispatchTask).where(
            AgentDispatchTask.task_id == task_id,
            AgentDispatchTask.user_id == user_id,
        )
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="MCP job not found")
    return row


async def _call_internal_mcp(runtime_url: str, req: MCPCallRequest, user_id: int) -> Any:
    from api.runtime.internal_http import internal_post

    try:
        return await internal_post(
            runtime_url,
            "/internal/mcp/call",
            json={
                "tool": req.tool,
                "user_id": user_id,
                "ai_config_id": req.ai_config_id,
                "arguments": req.arguments or {},
            },
            timeout=120.0,
        )
    except Exception as exc:
        response = getattr(exc, "response", None)
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code < 400 or status_code > 599:
            raise
        try:
            payload = response.json()
            detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
        except Exception:
            detail = "MCP runtime request failed"
        raise HTTPException(status_code=status_code, detail=detail) from exc


@router.get("/tools")
async def list_mcp_tools(
    ai_config_id: Optional[int] = Query(None),
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    # Keep the unscoped catalog path usable for library/admin callers.  When an
    # AI config is supplied this is replaced with the verified row below; when
    # it is omitted the response must still be deterministic rather than
    # reading an uninitialised local.
    cfg = None
    allowed_tools = None
    if ai_config_id is not None:
        cfg = session.exec(
            select(AssistantAIConfig).where(
                AssistantAIConfig.id == ai_config_id,
                AssistantAIConfig.user_id == user.id,
            )
        ).first()
        if not cfg:
            raise HTTPException(status_code=404, detail="AI config not found")
        from api.services.mcp.capability_view import scoped_tool_view_for_ids

        allowed_tools = set(scoped_tool_view_for_ids(user.id, ai_config_id).eligible_names)

    tools = registry.list_tools()
    for tool in tools:
        tool["description"] = str(tool.get("description") or "").strip()
        tool["inputSchema"] = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
    endpoint_defs = online_tool_defs()
    endpoint_tool_defs = [
        {
            "name": name,
            "description": str(spec.get("description") or "").strip(),
            "inputSchema": spec.get("input_schema") if isinstance(spec.get("input_schema"), dict) else {},
            "destructive": True,
            "mcpSource": str(spec.get("mcpSource") or "desktop"),
        }
        for name, spec in sorted(endpoint_defs.items())
    ]
    all_prompt_tools = [
        {
            **tool,
            "mcpSource": "server",
            "allowedForCurrentAi": allowed_tools is None or str(tool.get("name") or "") in allowed_tools,
        }
        for tool in tools
    ] + [
        {
            **tool,
            "allowedForCurrentAi": allowed_tools is None or str(tool.get("name") or "") in allowed_tools,
        }
        for tool in endpoint_tool_defs
    ]
    if allowed_tools is not None:
        all_prompt_tools = [
            tool for tool in all_prompt_tools
            if str(tool.get("name") or "") in allowed_tools
        ]
        known_prompt_names = {str(tool.get("name") or "") for tool in all_prompt_tools}
        for name in sorted(allowed_tools - known_prompt_names):
            all_prompt_tools.append({
                "name": name,
                "description": "",
                "inputSchema": {},
                "destructive": is_endpoint_agent_tool(name),
                "mcpSource": (
                    "workshop" if is_workshop_tool(name)
                    else "browser" if str(name).startswith(("browser_", "card_"))
                    else ("desktop" if is_endpoint_agent_tool(name) else "server")
                ),
                "allowedForCurrentAi": True,
            })

    prompt_tool_groups = build_prompt_tool_groups(
        user_id=user.id,
        ai_config_id=ai_config_id,
        prompt_tools=all_prompt_tools,
        allowed_tools=allowed_tools,
    )

    return {
        "tools": tools,
        # Endpoint (desktop / browser) tools currently advertised by connected
        # agents. Lets the UI list tools a desktop agent gained at runtime —
        # e.g. a Windows agent extended with new MCP tools — beyond the static
        # built-in lists baked into the web bundle.
        "endpointTools": connected_endpoint_tool_catalog(),
        "endpointToolDefs": endpoint_tool_defs,
        "promptTools": sorted(all_prompt_tools, key=lambda item: str(item.get("name") or "")),
        "promptToolGroups": prompt_tool_groups,
        "promptToolsScope": "current_ai" if ai_config_id is not None else "all_current",
        "promptToolsAiConfigId": ai_config_id,
        "promptToolsMcpEnabled": True if cfg is None else bool(cfg.mcp_enabled),
        "userId": user.id,
    }


@router.post("/call")
async def call_mcp_tool(
    req: MCPCallRequest,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    # 宽容解析工具名（mcp_xxx / mcp__xxx / 旧名 → 真实名），再做门禁与调用；
    # 模型/前端把 . 写成 _ 时不再直接报「未知工具」。
    try:
        from api.services.mcp.mcp_tool_aliases import apply_legacy_desktop_call, resolve_tool_name

        _cands = {str(t.get("name") or "").strip() for t in registry.list_tools() if t.get("name")}
        if req.ai_config_id is not None:
            _cands.update(endpoint_tools_for_config(req.ai_config_id, user.id))
            _cands.update(endpoint_bridge_tools_for_config(req.ai_config_id, user.id))
        original_tool = req.tool
        req.tool = resolve_tool_name(req.tool, _cands)
        req.arguments = apply_legacy_desktop_call(original_tool, req.tool, req.arguments or {})
    except Exception:
        pass
    _require_endpoint_ai_config(req.tool, req.ai_config_id)
    if req.ai_config_id is not None:
        cfg = session.exec(
            select(AssistantAIConfig).where(
                AssistantAIConfig.id == req.ai_config_id,
                AssistantAIConfig.user_id == user.id,
            )
        ).first()
        if not cfg:
            raise HTTPException(status_code=404, detail="AI config not found")
        if not cfg.mcp_enabled:
            raise HTTPException(status_code=400, detail="MCP is disabled for this AI")
        from api.services.mcp.capability_view import ensure_tool_eligible

        ensure_tool_eligible(user.id, req.ai_config_id, req.tool)
        if req.tool == "console.execute" and _console_target(req.arguments) == "device":
            endpoint_tool = _console_endpoint_tool(req.arguments)
            _require_endpoint_ai_config(endpoint_tool, req.ai_config_id)
            ensure_tool_eligible(user.id, req.ai_config_id, endpoint_tool)

    if req.tool == "console.execute" and _console_target(req.arguments) == "device":
        _require_endpoint_ai_config(_console_endpoint_tool(req.arguments), req.ai_config_id)
        return await call_mcp_or_endpoint_tool(
            req.tool,
            user.id,
            req.arguments or {},
            req.ai_config_id,
        )

    if is_endpoint_agent_tool(req.tool):
        # The Connector process owns live endpoint sockets in split deployments.
        # Reuse the runtime-aware dispatch path instead of consulting Gateway's
        # empty in-process agent registry.
        return await call_mcp_or_endpoint_tool(
            req.tool,
            user.id,
            req.arguments or {},
            req.ai_config_id,
        )

    # Search is a direct outbound API call and must not depend on the internal
    # MCP runtime port being reachable.
    if req.tool == "workspace.search":
        return await registry.call(req.tool, user.id, req.arguments, req.ai_config_id)

    # In split deployments, route via mcp-runtime so the user-facing test
    # path uses the same registry version the AI worker uses. Without this,
    # admins who reload mcp-runtime would still see stale tool behavior in
    # the UI "test tool" feature.
    from api.core.settings import settings
    runtime_url = settings.mcp_runtime_url
    if runtime_url:
        return await _call_internal_mcp(runtime_url, req, user.id)

    return await registry.call(req.tool, user.id, req.arguments, req.ai_config_id)


@router.post("/inheritance-test")
async def inheritance_mcp_test(
    req: InheritanceMcpTestRequest,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    """Let a configured model preset infer MCP args from schema text, then run on device."""
    user = get_current_user(authorization, session)
    _require_endpoint_ai_config(req.tool, req.ai_config_id)
    if req.ai_config_id is not None:
        cfg = session.exec(
            select(AssistantAIConfig).where(
                AssistantAIConfig.id == req.ai_config_id,
                AssistantAIConfig.user_id == user.id,
            )
        ).first()
        if not cfg:
            raise HTTPException(status_code=404, detail="AI config not found")
        if not cfg.mcp_enabled:
            raise HTTPException(status_code=400, detail="MCP is disabled for this AI")
        from api.services.mcp.capability_view import ensure_tool_eligible

        ensure_tool_eligible(user.id, req.ai_config_id, req.tool)
        _require_bound_inheritance_device(user.id, req.ai_config_id, req.device_id)
    try:
        return await run_inheritance_mcp_test(
            user=user,
            model_preset_id=req.model_preset_id,
            tool=req.tool,
            device_id=req.device_id,
            device_type=req.device_type,
            description=req.description,
            parameters=req.parameters,
            input_schema=req.input_schema,
            implementation={
                **(req.implementation or {}),
                "_heysure_ai_config_id": req.ai_config_id,
            },
            user_hint=req.user_hint,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/jobs")
async def create_mcp_job(
    req: MCPJobCreateRequest,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    """Queue one endpoint MCP call and return immediately with a job id."""
    user = get_current_user(authorization, session)
    tool = str(req.tool or "").strip()
    arguments = dict(req.arguments or {})
    if tool == "console.execute":
        if _console_target(arguments) != "device":
            raise HTTPException(
                status_code=400,
                detail="workspace console calls are synchronous; use target=device for an MCP job",
            )
        from tools.console import console_device_request

        tool, arguments = console_device_request(arguments)
    _require_endpoint_ai_config(tool, req.ai_config_id)
    if not is_endpoint_agent_tool(tool):
        raise HTTPException(status_code=400, detail="MCP jobs currently require an endpoint MCP tool")
    cfg = session.exec(
        select(AssistantAIConfig).where(
            AssistantAIConfig.id == req.ai_config_id,
            AssistantAIConfig.user_id == user.id,
        )
    ).first()
    if not cfg:
        raise HTTPException(status_code=404, detail="AI config not found")
    if not cfg.mcp_enabled:
        raise HTTPException(status_code=400, detail="MCP is disabled for this AI")
    from api.services.mcp.capability_view import ensure_tool_eligible

    ensure_tool_eligible(user.id, req.ai_config_id, tool)
    from connector_runtime.dispatch.device_dispatch import dispatch_endpoint_tool

    task_id = await dispatch_endpoint_tool(
        user_id=user.id,
        ai_config_id=req.ai_config_id,
        tool=tool,
        args=arguments,
        timeout_seconds=req.timeout_seconds,
    )
    if not task_id:
        raise HTTPException(status_code=503, detail="No connected endpoint agent for this tool")
    row = _owned_dispatch_row(session, user.id, task_id)
    return _dispatch_job_payload(row)


@router.get("/jobs/{task_id}")
def get_mcp_job(
    task_id: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    return _dispatch_job_payload(_owned_dispatch_row(session, user.id, task_id))


@router.post("/jobs/{task_id}/cancel")
async def cancel_mcp_job(
    task_id: str,
    req: Optional[MCPJobCancelRequest] = None,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    _owned_dispatch_row(session, user.id, task_id)
    from connector_runtime.dispatch.dispatch_cancellation import cancel_dispatch

    await cancel_dispatch(task_id, reason=(req.reason if req else "cancelled by caller"))
    session.expire_all()
    return _dispatch_job_payload(_owned_dispatch_row(session, user.id, task_id))


@router.post("/internal/reload", dependencies=[Depends(require_internal_token)])
def admin_reload_registry() -> Dict[str, Any]:
    """Reload MCP tools on the in-process registry.

    Admin-only via ``HEYSURE_INTERNAL_TOKEN`` Bearer header. End-user routes
    above remain user-scoped — this endpoint never touches per-user data,
    it only refreshes globally-shared tool code.
    """
    result = reload_registry()
    if not result.get("ok"):
        raise HTTPException(status_code=503, detail=result)
    return result
