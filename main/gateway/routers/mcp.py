"""MCP routes: list permitted tools for a config (``/tools``), execute a tool call
with permission checks (``/call``), and reload the tool registry (internal)."""

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from api.database import get_session
from api.devices.presence import online_tool_defs
from mcp_runtime.mcp import registry
from mcp_runtime.mcp.core import MCP_INTROSPECTION_TOOLS
from mcp_runtime.mcp.loader import reload_registry
from mcp_runtime.mcp.permissions import (
    CONFIGURABLE_ROLES,
    ROLE_LABELS_ZH,
    config_role_tier,
    default_role_permissions,
    effective_allowed_for_config,
    parse_role_permissions,
    role_tool_options,
    tool_min_role,
)
from api.models import AssistantAIConfig
from .auth import get_current_user
from api.runtime.internal_http import require_internal_token
from api.services.mcp.mcp_tool_runner import run_inheritance_mcp_test
from connector_runtime.dispatch.device_dispatch import dispatch_endpoint_tool_and_wait
from connector_runtime.dispatch.desktop_device_tools import (
    connected_endpoint_tool_catalog,
    endpoint_bridge_tools_for_config,
    endpoint_tools_for_config,
    is_endpoint_agent_tool,
    is_workshop_tool,
    strip_endpoint_tool_config_names,
)
from api.services.mcp.mcp_prompt_groups import build_prompt_tool_groups
from api.services.tasks.task_system import with_workspace_read_by_name_compat

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


@router.get("/tools")
async def list_mcp_tools(
    ai_config_id: Optional[int] = Query(None),
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
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
        if cfg.mcp_enabled:
            try:
                parsed_allowed = json.loads(cfg.mcp_tools or "[]")
                if not isinstance(parsed_allowed, list):
                    raise ValueError("mcp_tools must be a JSON array")
                allowed_tools = {str(item).strip() for item in parsed_allowed if isinstance(item, str) and str(item).strip()}
                allowed_tools = strip_endpoint_tool_config_names(with_workspace_read_by_name_compat(allowed_tools))
                allowed_tools.update(MCP_INTROSPECTION_TOOLS)
                allowed_tools.update(endpoint_bridge_tools_for_config(ai_config_id, user.id))
                allowed_tools.update(endpoint_tools_for_config(ai_config_id, user.id))
            except Exception:
                raise HTTPException(status_code=400, detail="Invalid AI MCP tool config")
        else:
            allowed_tools = set()

    # Server toolbox tools are now primarily granted via the bound toolbox's DeviceMcpScope
    # (edited in 工具箱 MCP 权限). Augment here so that /tools and catalog reflect the
    # actual selectable tools after binding. This prevents conflicts with legacy cfg.mcp_tools.
    if ai_config_id is not None and (cfg is None or getattr(cfg, 'mcp_enabled', True)):
        try:
            from connector_runtime.dispatch.desktop_device_tools import toolbox_tools_for_config
            tb = toolbox_tools_for_config(ai_config_id, user.id)
            if allowed_tools is None:
                allowed_tools = set()
            allowed_tools |= tb
        except Exception:
            pass

    tools = registry.list_tools()
    for tool in tools:
        name = str(tool.get("name") or "")
        tool["minRole"] = tool_min_role(name)
        tool["description"] = str(tool.get("description") or "").strip()
        tool["inputSchema"] = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
    tool_names = {str(tool.get("name") or "") for tool in tools}
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
        "roleOrder": CONFIGURABLE_ROLES,
        "roleLabels": ROLE_LABELS_ZH,
        # Per-role defaults for checked state and reset-to-default.
        "roleDefaults": default_role_permissions(tool_names),
        # Per-role visible/configurable options. This may include tools that are
        # not checked by default.
        "roleOptions": role_tool_options(tool_names),
        # The admin's currently configured per-role allow-list (may be empty,
        # meaning "use defaults").
        "rolePermissions": parse_role_permissions(user),
    }


@router.post("/call")
async def call_mcp_tool(
    req: MCPCallRequest,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
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
        try:
            parsed_allowed = json.loads(cfg.mcp_tools or "[]")
            if not isinstance(parsed_allowed, list):
                raise ValueError("mcp_tools must be a JSON array")
            allowed_tools = {str(item).strip() for item in parsed_allowed if isinstance(item, str) and str(item).strip()}
            allowed_tools = strip_endpoint_tool_config_names(with_workspace_read_by_name_compat(allowed_tools))
            allowed_tools.update(MCP_INTROSPECTION_TOOLS)
            allowed_tools.update(endpoint_bridge_tools_for_config(req.ai_config_id, user.id))
            allowed_tools.update(endpoint_tools_for_config(req.ai_config_id, user.id))
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid AI MCP tool config")
        if req.tool not in allowed_tools:
            raise HTTPException(status_code=403, detail=f"Tool not allowed for this AI: {req.tool}")

        # Augment with toolbox scope (server toolbox tools are now sourced from the toolbox DeviceMcpScope)
        try:
            from connector_runtime.dispatch.desktop_device_tools import toolbox_tools_for_config
            allowed_tools |= toolbox_tools_for_config(req.ai_config_id, user.id)
        except Exception:
            pass

        # Enforce the role ceiling: known registry tools must be within the set
        # permitted for this AI's role tier, regardless of its saved allow-list.
        role_allowed = effective_allowed_for_config(user, cfg)
        bridge_tools = endpoint_bridge_tools_for_config(req.ai_config_id, user.id)
        if registry.has(req.tool) and req.tool not in role_allowed and req.tool not in bridge_tools:
            raise HTTPException(
                status_code=403,
                detail=f"Tool not permitted for role {config_role_tier(cfg)}: {req.tool}",
            )

    if is_endpoint_agent_tool(req.tool):
        return {
            "tool": req.tool,
            "destructive": True,
            "result": await dispatch_endpoint_tool_and_wait(
                user_id=user.id,
                ai_config_id=req.ai_config_id,
                tool=req.tool,
                args=req.arguments or {},
            ),
        }

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
        from api.runtime.internal_http import internal_post
        return await internal_post(
            runtime_url,
            "/internal/mcp/call",
            json={
                "tool": req.tool,
                "user_id": user.id,
                "ai_config_id": req.ai_config_id,
                "arguments": req.arguments or {},
            },
            timeout=120.0,
        )

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
