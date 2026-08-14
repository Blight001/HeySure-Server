"""``/api/devices`` routes: list connected endpoint agents, bind an agent to an AI
config, get/set an agent's per-device MCP tool scope, and read/edit the device
developer manual shown in the web console."""

import time
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from api.devices.bindings import get_bindings, set_binding, set_member_binding
from api.devices.mcp_permissions import get_scope, reconcile_scope_with_capabilities, set_scope
from api.devices.presence import online_agent_snapshot_for_user
from api.database import get_session
from api.models import AssistantAIConfig, DevicePresence
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
    # Split deployment: endpoint sockets belong to Connector Runtime, so the
    # Gateway's process-local ``agents`` registry is empty for those devices.
    # Fall back to the shared presence snapshot written by the socket owner.
    return online_agent_snapshot_for_user(user_id, aid)


def _ensure_scope_member(
    session: Session,
    user_id: int,
    device_id: str,
    ai_config_id: int,
) -> None:
    cfg = session.exec(
        select(AssistantAIConfig).where(
            AssistantAIConfig.id == int(ai_config_id),
            AssistantAIConfig.user_id == user_id,
        )
    ).first()
    if not cfg:
        raise HTTPException(status_code=404, detail="AI 配置不存在或不属于当前用户")
    try:
        from library import engine as workshop_engine
        from tools import engine as toolbox_engine
        from api.devices.workshop_bindings import bound_config_ids_for_agent

        is_builtin = (
            workshop_engine.is_builtin_workshop_device_id(device_id)
            or device_id == toolbox_engine.toolbox_device_id_for_user(user_id)
        )
        bound_ids = (
            bound_config_ids_for_agent(user_id, device_id)
            if is_builtin
            else set(get_bindings(user_id, device_id))
        )
    except Exception:
        bound_ids = set(get_bindings(user_id, device_id))
    if int(ai_config_id) not in bound_ids:
        raise HTTPException(status_code=409, detail="该 AI 成员尚未绑定此设备")


def _scope_ai_config_id(agent: dict, requested: Optional[int]) -> Optional[int]:
    if requested is not None:
        return requested
    try:
        value = agent.get("aiConfigId") or agent.get("ai_config_id")
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def _scope_tool_defs(device_type: str, user_id: int, device_id: str) -> dict:
    try:
        from api.devices.presence import tool_defs_for_agent

        definitions = tool_defs_for_agent(user_id, device_id)
    except Exception:
        definitions = {}
    if definitions or device_type not in ("workshop", "toolbox"):
        return definitions
    try:
        if device_type == "workshop":
            from library import engine as builtin_engine
        else:
            from tools import engine as builtin_engine
        return (
            builtin_engine.tool_defs_map()
            if device_type == "workshop"
            else builtin_engine.toolbox_tool_defs_map()
        )
    except Exception:
        return {}


def _scope_view(agent: dict, user_id: int, ai_config_id: Optional[int] = None) -> dict:
    """Capabilities + effective allow-list for a connected agent. Scope is keyed
    per individual agent. Reconcile on (re)connect (for any device type) now
    ensures the persisted scope row always contains the *full* live capabilities,
    so UI + grants default to all checked (new MCPs auto-included on reconnect).
    A missing row (very first before reconcile) yields hasRecord=false + full.
    """
    device_type = device_type_of(agent)
    device_id = str(agent.get("id") or "")
    capabilities = sorted(agent_endpoint_tools(agent))
    ai_config_id = _scope_ai_config_id(agent, ai_config_id)
    scope = get_scope(user_id, device_id, ai_config_id) if device_id else None
    # Reconcile ensures full for existing rows too (newly added MCPs appear).
    # hasRecord=true after reconcile; UI falls back to caps only for truly new.
    if scope is None:
        allowed = capabilities
    else:
        allowed = sorted(set(capabilities) & scope)
    tool_defs = _scope_tool_defs(device_type, user_id, device_id)
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
def list_connected_devices(
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


class DeviceMemberBindingRequest(BaseModel):
    bound: bool = True


class DeviceDisplayRequest(BaseModel):
    remark: str = ""
    icon: str = ""
    aiDescriptionOverride: str = ""


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
            raise HTTPException(status_code=400, detail="图书馆请通过 /api/devices/builtin-bindings 绑定")
    except HTTPException:
        raise
    except Exception:
        pass
    cfg_id = payload.aiConfigId
    if cfg_id:
        cfg_id = int(cfg_id)
        cfg = session.exec(
            select(AssistantAIConfig).where(AssistantAIConfig.id == cfg_id)
        ).first()
        if not cfg or cfg.user_id != user.id:
            raise HTTPException(status_code=404, detail="AI 配置不存在或不属于当前用户")

    # Legacy clients still replace the whole set with zero or one member.
    stored = set_binding(user.id, device_id, cfg_id)
    bound_ids = get_bindings(user.id, device_id)

    # Reflect the assignment on any live socket(s) for this agent right away so
    # the next dispatch routes correctly without waiting for a reconnect.
    for agent in agents.values():
        if str(agent.get("id")) == device_id and agent.get("userId") == user.id:
            agent["aiConfigId"] = stored
            agent["boundAiConfigIds"] = bound_ids

    # Keep the shared DB presence snapshot in sync so off-gateway processes
    # resolve endpoint tools against the new assignment immediately.
    try:
        from api.devices.presence import update_binding
        update_binding(device_id, stored)
    except Exception:
        pass

    await emit_agent_list_for_user(user.id)
    return {
        "ok": True,
        "deviceId": device_id,
        "aiConfigId": stored,
        "boundAiConfigIds": bound_ids,
    }


@router.put("/{device_id}/member-bindings/{ai_config_id}")
async def set_agent_member_binding(
    device_id: str,
    ai_config_id: int,
    payload: DeviceMemberBindingRequest,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    """Add/remove one AI member while preserving every other assignment."""
    user = get_current_user(authorization, session)
    aid = str(device_id or "").strip()
    if not aid:
        raise HTTPException(status_code=400, detail="deviceId required")
    cfg = session.exec(
        select(AssistantAIConfig).where(
            AssistantAIConfig.id == int(ai_config_id),
            AssistantAIConfig.user_id == user.id,
        )
    ).first()
    if not cfg:
        raise HTTPException(status_code=404, detail="AI 配置不存在或不属于当前用户")
    try:
        from library import engine as workshop_engine
        from tools import engine as toolbox_engine

        if workshop_engine.is_builtin_workshop_device_id(aid) or aid == toolbox_engine.toolbox_device_id_for_user(user.id):
            raise HTTPException(status_code=400, detail="内置设备请通过 /api/devices/builtin-bindings 绑定")
    except HTTPException:
        raise
    except Exception:
        pass

    bound_ids = set_member_binding(user.id, aid, cfg.id, bound=payload.bound)
    primary = bound_ids[0] if bound_ids else None
    agent = _find_connected_agent(aid, user.id)
    if agent and payload.bound:
        capabilities = agent_endpoint_tools(agent)
        reconcile_scope_with_capabilities(
            user.id,
            aid,
            capabilities,
            ai_config_id=cfg.id,
            device_type=device_type_of(agent),
        )
    for live_agent in agents.values():
        if str(live_agent.get("id")) == aid and live_agent.get("userId") == user.id:
            live_agent["aiConfigId"] = primary
            live_agent["boundAiConfigIds"] = bound_ids
    try:
        from api.devices.presence import update_binding

        update_binding(aid, primary)
    except Exception:
        pass
    await emit_agent_list_for_user(user.id)
    return {
        "ok": True,
        "deviceId": aid,
        "aiConfigId": primary,
        "boundAiConfigIds": bound_ids,
    }


@router.put("/{device_id}/display")
async def update_device_display(
    device_id: str,
    payload: DeviceDisplayRequest,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    """Persist operator-authored display settings for one endpoint device.

    These settings are separate from the values reported in ``device:register``
    so reconnects keep the user's remark/icon choice.
    """
    user = get_current_user(authorization, session)
    aid = (device_id or "").strip()
    if not aid:
        raise HTTPException(status_code=400, detail="deviceId required")
    try:
        from library import engine as workshop_engine
        from tools import engine as toolbox_engine

        if workshop_engine.is_builtin_workshop_device_id(aid) or aid == toolbox_engine.toolbox_device_id_for_user(user.id):
            raise HTTPException(status_code=400, detail="系统内置设备不支持自定义显示")
    except HTTPException:
        raise
    except Exception:
        pass

    agent = _find_connected_agent(aid, user.id)
    row = session.exec(
        select(DevicePresence).where(
            DevicePresence.user_id == user.id,
            DevicePresence.device_id == aid,
        ).order_by(DevicePresence.updated_at.desc(), DevicePresence.id.desc())
    ).first()
    if not row:
        if not agent:
            raise HTTPException(status_code=404, detail="设备记录不存在")
        row = DevicePresence(
            user_id=user.id,
            device_id=aid,
            device_type=device_type_of(agent) or "custom",
            name=str(agent.get("name") or "").strip(),
            platform=str(agent.get("platform") or "").strip(),
            icon=str(agent.get("icon") or "").strip(),
            online=True,
        )
        session.add(row)
    device_type = str(row.device_type or device_type_of(agent) or "").strip()
    if device_type in ("workshop", "toolbox"):
        raise HTTPException(status_code=400, detail="系统内置设备不支持自定义显示")

    from api.devices.catalog import normalize_ai_description
    from api.devices.presence import (
        device_remark_value,
        effective_ai_description,
        effective_device_icon,
        normalize_device_icon,
    )

    row.remark = device_remark_value(payload.remark)
    row.icon_override = normalize_device_icon(payload.icon)
    row.ai_description_override = normalize_ai_description(payload.aiDescriptionOverride)
    row.updated_at = time.time()
    session.add(row)
    session.commit()
    await emit_agent_list_for_user(user.id)
    return {
        "ok": True,
        "deviceId": aid,
        "remark": row.remark,
        "icon": effective_device_icon(row),
        "iconOverride": row.icon_override,
        "reportedAiDescription": row.reported_ai_description,
        "aiDescriptionOverride": row.ai_description_override,
        "effectiveAiDescription": effective_ai_description(row),
        "catalogGeneration": row.catalog_generation,
        "catalogHash": row.catalog_hash,
        "catalogProtocolVersion": row.catalog_protocol_version,
    }


@router.delete("/{device_id}")
async def forget_device(
    device_id: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    """Delete the persisted record (AI binding + presence + saved MCP scope)
    for a device that isn't connected right now. Only meaningful for offline
    devices — a live one would just recreate its presence row on its next
    heartbeat, so this is refused while the device is connected."""
    user = get_current_user(authorization, session)
    aid = (device_id or "").strip()
    if not aid:
        raise HTTPException(status_code=400, detail="deviceId required")
    if _find_connected_agent(aid, user.id):
        raise HTTPException(status_code=400, detail="设备在线时无法删除记录，请等待其离线后再试")

    set_binding(user.id, aid, None)

    presence_rows = session.exec(
        select(DevicePresence).where(DevicePresence.device_id == aid)
    ).all()
    deleted = False
    for row in presence_rows:
        if row.user_id == user.id:
            session.delete(row)
            deleted = True
    if deleted:
        session.commit()

    from api.devices.mcp_permissions import delete_scope
    delete_scope(user.id, aid)

    await emit_agent_list_for_user(user.id)
    return {"ok": True, "deviceId": aid, "deleted": deleted}


# ── 设备开发手册（设备栏目"设备端开发文档"弹窗） ──────────────────────────
# 默认内容随服务端打包（server/static/device_dev_manual.md，与 device/read.md
# 同源）；房主在控制台编辑后存入 SystemSetting，清空保存即恢复默认。
DEV_MANUAL_SETTING_KEY = "devices.dev_manual_md"
_DEV_MANUAL_DEFAULT_PATH = Path(__file__).resolve().parents[3] / "static" / "device_dev_manual.md"


def _default_dev_manual() -> str:
    try:
        return _DEV_MANUAL_DEFAULT_PATH.read_text(encoding="utf-8")
    except Exception:
        return "# 设备开发手册\n\n默认文档缺失（server/static/device_dev_manual.md）。"


@router.get("/dev-manual")
def get_device_dev_manual(
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    get_current_user(authorization, session)
    from api.models import SystemSetting

    row = session.get(SystemSetting, DEV_MANUAL_SETTING_KEY)
    if row and str(row.value).strip():
        return {"content": row.value, "isCustom": True, "updatedAt": row.updated_at}
    return {"content": _default_dev_manual(), "isCustom": False, "updatedAt": None}


class DeviceDevManualRequest(BaseModel):
    content: str = ""


@router.put("/dev-manual")
def save_device_dev_manual(
    payload: DeviceDevManualRequest,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    """Persist the operator-edited manual. Saving empty content resets to the
    bundled default."""
    get_current_user(authorization, session)
    from api.models import SystemSetting

    content = str(payload.content or "")
    row = session.get(SystemSetting, DEV_MANUAL_SETTING_KEY)
    if not content.strip():
        if row is not None:
            session.delete(row)
            session.commit()
        return {"content": _default_dev_manual(), "isCustom": False, "updatedAt": None}
    if row is None:
        row = SystemSetting(key=DEV_MANUAL_SETTING_KEY, value=content)
        session.add(row)
    else:
        row.value = content
        row.updated_at = time.time()
    session.commit()
    return {"content": content, "isCustom": True, "updatedAt": row.updated_at}


@router.get("/{device_id}/mcp-scope")
def get_agent_mcp_scope(
    device_id: str,
    ai_config_id: Optional[int] = None,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    """Endpoint MCP permission scope for one connected agent (any type).

    Returns the tools it advertises plus the currently-allowed subset (reconcile
    on register ensures full live caps so new devices + new MCPs default checked).
    404 when the device is offline."""
    user = get_current_user(authorization, session)
    agent = _find_connected_agent(device_id, user.id)
    if not agent:
        raise HTTPException(status_code=404, detail="设备未连接")
    if not device_type_of(agent):
        raise HTTPException(status_code=400, detail="该设备不是可管理的端点 Agent（不支持 MCP 范围管理）")
    if ai_config_id is not None:
        _ensure_scope_member(session, user.id, device_id, ai_config_id)
    return _scope_view(agent, user.id, ai_config_id)


class DeviceMcpScopeRequest(BaseModel):
    tools: List[str] = []
    aiConfigId: Optional[int] = None


@router.put("/{device_id}/mcp-scope")
async def set_agent_mcp_scope(
    device_id: str,
    payload: DeviceMcpScopeRequest,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    """Persist one bound AI member's MCP scope for a connected device."""
    user = get_current_user(authorization, session)
    agent = _find_connected_agent(device_id, user.id)
    if not agent:
        raise HTTPException(status_code=404, detail="设备未连接")
    device_type = device_type_of(agent)
    if not device_type:
        raise HTTPException(status_code=400, detail="该设备不是端点 Agent（不支持 MCP 范围管理）")

    ai_config_id = payload.aiConfigId
    if ai_config_id is None:
        ai_config_id = agent.get("aiConfigId") or agent.get("ai_config_id")
    try:
        ai_config_id = int(ai_config_id) if ai_config_id else None
    except (TypeError, ValueError):
        ai_config_id = None
    if ai_config_id:
        _ensure_scope_member(session, user.id, device_id, ai_config_id)

    # Only persist tools the agent actually reports — never let stale UI state
    # widen the scope beyond the live capability set.
    capabilities = agent_endpoint_tools(agent)
    requested = {str(t).strip() for t in (payload.tools or []) if str(t).strip()}
    set_scope(user.id, device_id, requested & capabilities, ai_config_id=ai_config_id, device_type=device_type)

    await emit_agent_list_for_user(user.id)
    return _scope_view(agent, user.id, ai_config_id)
