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


class MCPCallRequest(BaseModel):
    tool: str = Field(..., description="Fully qualified MCP tool name")
    arguments: Optional[Dict[str, Any]] = Field(default_factory=dict)
    ai_config_id: Optional[int] = None


class InheritanceMcpTestRequest(BaseModel):
    model_preset_id: str = Field(..., description="Server model preset id from user.model_presets")
    tool: str
    device_id: str
    device_type: str = "desktop"
    description: str = ""
    parameters: Optional[List[Dict[str, Any]]] = None
    input_schema: Optional[Dict[str, Any]] = None
    implementation: Optional[Dict[str, Any]] = None
    user_hint: str = ""


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
            implementation=req.implementation,
            user_hint=req.user_hint,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
