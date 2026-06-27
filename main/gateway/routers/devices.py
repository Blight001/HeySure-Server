"""``/api/devices`` routes: list connected endpoint agents, bind an agent to an AI
config, and get/set an agent's per-device MCP tool scope."""

from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from api.devices.bindings import set_binding
from api.devices.mcp_permissions import get_scope, set_scope
from api.database import get_session
from api.models import DeviceAiBinding, AssistantAIConfig, DevicePresence
from .auth import get_current_user
from api.sio import agents, device_token_required
from api.devices.live import connected_agent_rows_for_user, emit_agent_list_for_user
from connector_runtime.dispatch.desktop_device_tools import agent_endpoint_tools, device_type_of

router = APIRouter()
PREFIX = "/api/devices"


def _find_connected_agent(device_id: str, user_id: int) -> Optional[dict]:
    """The live agent record for this (device_id, user), or None when the device
    is not currently connected. Scope is only visible while connected — a
    disconnected agent is simply not shown.

    内置图书馆不走 socket，按需合成一条常在线虚拟记录。"""
    aid = str(device_id or "").strip()
    for agent in agents.values():
        if str(agent.get("id") or "") == aid and agent.get("userId") == user_id:
            return agent
    try:
        from tools import engine as toolbox_engine
        from library import engine as workshop_engine

        if aid == workshop_engine.device_id_for_user(user_id):
            return workshop_engine.connected_entry_for_user(user_id)
        if aid == toolbox_engine.toolbox_device_id_for_user(user_id):
            return toolbox_engine.toolbox_connected_entry_for_user(user_id)
    except Exception:
        pass
    return None


def _presence_device_type(session: Session, user_id: int, device_id: str) -> Optional[str]:
    aid = str(device_id or "").strip()
    if not aid:
        return None
    row = session.exec(
        select(DevicePresence)
        .where(
            DevicePresence.device_id == aid,
            DevicePresence.user_id == user_id,
        )
        .order_by(DevicePresence.updated_at.desc(), DevicePresence.id.desc())
    ).first()
    device_type = str(row.device_type or "").strip() if row else ""
    return device_type if device_type in {"desktop", "browser", "android", "workshop", "toolbox"} else None


def _device_type_for_binding(session: Session, user_id: int, device_id: str) -> Optional[str]:
    connected = _find_connected_agent(device_id, user_id)
    live_type = device_type_of(connected)
    if live_type:
        return live_type
    return _presence_device_type(session, user_id, device_id)


def _existing_same_type_binding(
    session: Session,
    *,
    user_id: int,
    ai_config_id: int,
    device_id: str,
    device_type: str,
) -> Optional[str]:
    """Return another bound agent id for this AI/type, if one exists."""
    target_id = str(device_id or "").strip()
    rows = session.exec(
        select(DeviceAiBinding).where(
            DeviceAiBinding.user_id == user_id,
            DeviceAiBinding.ai_config_id == ai_config_id,
        )
    ).all()
    for row in rows:
        existing_id = str(row.device_id or "").strip()
        if not existing_id or existing_id == target_id:
            continue
        if _device_type_for_binding(session, user_id, existing_id) == device_type:
            return existing_id
    return None


def _scope_view(agent: dict, user_id: int) -> dict:
    """Capabilities + effective allow-list for a connected agent. Scope is keyed
    per individual agent; with no saved record no endpoint tool is allowed
    (default-closed)."""
    device_type = device_type_of(agent)
    device_id = str(agent.get("id") or "")
    capabilities = sorted(agent_endpoint_tools(agent))
    ai_config_id = agent.get("aiConfigId") or agent.get("ai_config_id")
    try:
        ai_config_id = int(ai_config_id) if ai_config_id else None
    except (TypeError, ValueError):
        ai_config_id = None
    scope = get_scope(user_id, device_id) if device_id else None
    allowed = [] if scope is None else sorted(set(capabilities) & scope)
    try:
        from api.devices.presence import tool_defs_for_agent

        tool_defs = tool_defs_for_agent(user_id, device_id)
    except Exception:
        tool_defs = {}
    if device_type == "workshop" and not tool_defs:
        try:
            from library import engine as workshop_engine

            tool_defs = workshop_engine.tool_defs_map()
        except Exception:
            tool_defs = {}
    if device_type == "toolbox" and not tool_defs:
        try:
            from tools import engine as toolbox_engine

            tool_defs = toolbox_engine.toolbox_tool_defs_map()
        except Exception:
            tool_defs = {}
    return {
        "deviceId": device_id,
        "agentName": str(agent.get("name") or agent.get("id") or ""),
        "deviceType": device_type,
        "platform": str(agent.get("platform") or ""),
        "aiConfigId": ai_config_id,
        "capabilities": capabilities,
        "toolDefs": {name: tool_defs.get(name, {}) for name in capabilities},
        "allowed": allowed,
        "hasRecord": scope is not None,
    }


@router.get("/connected")
async def list_connected_devices(
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    # Auth-gate the view; the agent registry itself is process-global.
    user = get_current_user(authorization, session)
    rows = connected_agent_rows_for_user(user.id)
    return {
        "agents": rows,
        "count": len(rows),
        "token_required": device_token_required(),
    }


class DeviceBindRequest(BaseModel):
    deviceId: str
    # None / 0 unbinds the device (sets it back to "未分配").
    aiConfigId: Optional[int] = None


@router.post("/bind")
async def bind_agent_ai(
    payload: DeviceBindRequest,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    """Assign (or clear) the server-side AI for a connected device.

    Devices register without choosing an AI; the operator picks one here. The
    binding is persisted (keyed by agent id) so it survives reconnects, and any
    currently-connected socket for that agent is updated immediately.
    """
    user = get_current_user(authorization, session)
    device_id = (payload.deviceId or "").strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="deviceId required")
    try:
        from library import engine as workshop_engine

        if workshop_engine.is_builtin_workshop_device_id(device_id):
            raise HTTPException(status_code=400, detail="图书馆请通过 /api/workshop/bindings 绑定")
    except HTTPException:
        raise
    except Exception:
        pass
    cfg_id = payload.aiConfigId
    previous_same_type_device_id = None
    if cfg_id:
        cfg_id = int(cfg_id)
        cfg = session.exec(
            select(AssistantAIConfig).where(AssistantAIConfig.id == cfg_id)
        ).first()
        if not cfg or cfg.user_id != user.id:
            raise HTTPException(status_code=404, detail="AI 配置不存在或不属于当前用户")
        device_type = _device_type_for_binding(session, user.id, device_id)
        if device_type:
            existing_device_id = _existing_same_type_binding(
                session,
                user_id=user.id,
                ai_config_id=cfg_id,
                device_id=device_id,
                device_type=device_type,
            )
            if existing_device_id:
                previous_same_type_device_id = existing_device_id

    stored = set_binding(user.id, device_id, cfg_id)
    if previous_same_type_device_id:
        set_binding(user.id, previous_same_type_device_id, None)

    # Reflect the assignment on any live socket(s) for this agent right away so
    # the next dispatch routes correctly without waiting for a reconnect.
    for agent in agents.values():
        if str(agent.get("id")) == device_id and agent.get("userId") == user.id:
            agent["aiConfigId"] = stored
        elif (
            previous_same_type_device_id
            and str(agent.get("id")) == previous_same_type_device_id
            and agent.get("userId") == user.id
        ):
            agent["aiConfigId"] = None

    # Keep the shared DB presence snapshot in sync so off-gateway processes
    # resolve endpoint tools against the new assignment immediately.
    try:
        from api.devices.presence import update_binding
        update_binding(device_id, stored)
        if previous_same_type_device_id:
            update_binding(previous_same_type_device_id, None)
    except Exception:
        pass

    await emit_agent_list_for_user(user.id)
    return {"ok": True, "deviceId": device_id, "aiConfigId": stored}


@router.get("/{device_id}/mcp-scope")
async def get_agent_mcp_scope(
    device_id: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    """Endpoint MCP permission scope for one connected agent.

    Returns the tools it advertises plus the currently-allowed subset. 404 when
    the device is offline (an unbound agent still resolves, with aiConfigId
    null and every tool allowed — it just can't be persisted until assigned)."""
    user = get_current_user(authorization, session)
    agent = _find_connected_agent(device_id, user.id)
    if not agent:
        raise HTTPException(status_code=404, detail="设备未连接")
    if not device_type_of(agent):
        raise HTTPException(status_code=400, detail="该设备不是可管理的端点 Agent（不支持 MCP 范围管理）")
    return _scope_view(agent, user.id)


class DeviceMcpScopeRequest(BaseModel):
    tools: List[str] = []


@router.put("/{device_id}/mcp-scope")
async def set_agent_mcp_scope(
    device_id: str,
    payload: DeviceMcpScopeRequest,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    """Persist the endpoint MCP permission scope for a connected agent, keyed per
    individual agent (user, device_id). Unknown tool names are dropped; the scope
    follows the physical device across reconnects and AI reassignment."""
    user = get_current_user(authorization, session)
    agent = _find_connected_agent(device_id, user.id)
    if not agent:
        raise HTTPException(status_code=404, detail="设备未连接")
    device_type = device_type_of(agent)
    if not device_type:
        raise HTTPException(status_code=400, detail="该设备不是端点 Agent（不支持 MCP 范围管理）")

    ai_config_id = agent.get("aiConfigId") or agent.get("ai_config_id")
    try:
        ai_config_id = int(ai_config_id) if ai_config_id else None
    except (TypeError, ValueError):
        ai_config_id = None
    # The bound AI is recorded for reference only; scope is keyed by the agent.
    if ai_config_id:
        cfg = session.exec(
            select(AssistantAIConfig).where(AssistantAIConfig.id == ai_config_id)
        ).first()
        if not cfg or cfg.user_id != user.id:
            raise HTTPException(status_code=404, detail="AI 配置不存在或不属于当前用户")

    # Only persist tools the agent actually reports — never let stale UI state
    # widen the scope beyond the live capability set.
    capabilities = agent_endpoint_tools(agent)
    requested = {str(t).strip() for t in (payload.tools or []) if str(t).strip()}
    set_scope(user.id, device_id, requested & capabilities, ai_config_id=ai_config_id, device_type=device_type)

    await emit_agent_list_for_user(user.id)
    return _scope_view(agent, user.id)
