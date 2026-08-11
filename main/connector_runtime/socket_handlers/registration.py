"""Authenticated, idempotent endpoint-device registration."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from sqlmodel import Session, select
from pydantic import ValidationError

from api.database import engine
from api.devices.bindings import get_binding
from api.devices.live import emit_agent_list_for_user
from api.models import AssistantAIConfig
from api.sio import agents, is_agent_shared_secret, resolve_agent_user, sio
from connector_runtime.socket_handlers.schemas import AgentRegistrationPayload, validated_payload


logger = logging.getLogger(__name__)


@dataclass
class Registration:
    sid: str
    info: Dict[str, Any]
    device_id: str
    user_id: Optional[int]
    account: Optional[str]
    ai_config_id: Optional[int]


def _ai_belongs_to_user(ai_config_id: object, user_id: int) -> bool:
    if ai_config_id in (None, "", 0):
        return True
    try:
        config_id = int(ai_config_id)
    except (TypeError, ValueError):
        return False
    with Session(engine) as session:
        config = session.exec(
            select(AssistantAIConfig).where(AssistantAIConfig.id == config_id)
        ).first()
    return bool(config and config.user_id == user_id)


async def _authenticate(sid: str, info: Dict[str, Any]) -> tuple[bool, Optional[int], Optional[str]]:
    token = info.get("token")
    if is_agent_shared_secret(token):
        try:
            user_id = int(info["userId"]) if info.get("userId") is not None else None
        except (TypeError, ValueError):
            user_id = None
        return True, user_id, None
    resolved = resolve_agent_user(token)
    if resolved:
        return True, resolved[0], resolved[1]
    logger.info("Agent registration rejected (no auth): %s", info.get("id"))
    await sio.emit(
        "device:register_rejected",
        {"reason": "agent must be logged in (invalid or missing user token)"},
        to=sid,
    )
    return False, None, None


async def _registration(sid: str, info: Dict[str, Any]) -> Optional[Registration]:
    accepted, user_id, account = await _authenticate(sid, info)
    if not accepted:
        return None
    device_id = str(info.get("id") or sid)
    bound_ai = get_binding(user_id, device_id) if user_id is not None else None
    if user_id is not None and not _ai_belongs_to_user(bound_ai, user_id):
        logger.warning(
            "Agent registration rejected (AI ownership mismatch): agent=%s user=%s ai=%s",
            info.get("id"), user_id, bound_ai,
        )
        await sio.emit(
            "device:register_rejected",
            {"reason": "selected AI does not belong to the logged-in user"},
            to=sid,
        )
        return None
    return Registration(sid, info, device_id, user_id, account, bound_ai)


def _store_live_agent(ctx: Registration) -> None:
    from api.devices.presence import normalize_device_icon

    for old_sid in [
        key for key, agent in agents.items()
        if str(agent.get("id")) == ctx.device_id and key != ctx.sid
    ]:
        del agents[old_sid]
    info = dict(ctx.info)
    info.pop("token", None)
    now = time.time()
    agents[ctx.sid] = {
        **info,
        "id": ctx.device_id,
        "icon": normalize_device_icon(info.get("icon")),
        "aiConfigId": ctx.ai_config_id,
        "socketId": ctx.sid,
        "userId": ctx.user_id,
        "userAccount": ctx.account,
        "capabilities": info.get("capabilities") or [],
        "version": info.get("version") or "",
        "lifecycle": info.get("lifecycle") or "registered",
        "connectedAt": now,
        "lastSeenAt": now,
        "lastTaskId": None,
        "lastTaskStatus": None,
        "lastTaskAt": None,
        "lastError": None,
        "source": "socket",
        "dispatchable": True,
    }


def _record_presence(ctx: Registration) -> bool:
    from api.devices.mcp_permissions import (
        get_scope,
        reconcile_saved_scope_for_capability_change,
        reconcile_scope_with_capabilities,
        saved_scope_was_full,
        set_scope,
    )
    from api.devices.presence import capabilities_for_device, upsert_presence
    from connector_runtime.dispatch.desktop_device_tools import (
        agent_endpoint_tool_defs,
        agent_endpoint_tools,
        device_type_of,
    )

    agent = agents[ctx.sid]
    device_type = device_type_of(agent)
    if not device_type:
        return False
    agent["deviceType"] = device_type
    capabilities = sorted(agent_endpoint_tools(agent))
    previous_full = False
    if ctx.user_id is not None:
        previous = capabilities_for_device(ctx.user_id, ctx.device_id)
        saved = get_scope(ctx.user_id, ctx.device_id)
        previous_full = saved_scope_was_full(saved, previous)
        if saved is not None:
            expanded = reconcile_saved_scope_for_capability_change(saved, set(capabilities), previous)
            if expanded != saved:
                set_scope(
                    ctx.user_id, ctx.device_id, expanded,
                    ai_config_id=ctx.ai_config_id, device_type=device_type,
                )
    upsert_presence(
        ctx.user_id, ctx.device_id, ctx.ai_config_id, device_type, capabilities,
        online=True, tool_defs=agent_endpoint_tool_defs(agent),
        name=agent.get("name"), platform=agent.get("platform"), icon=agent.get("icon") or "",
    )
    if ctx.user_id is not None:
        reconcile_scope_with_capabilities(
            ctx.user_id, ctx.device_id, capabilities,
            ai_config_id=ctx.ai_config_id, device_type=device_type,
        )
    return previous_full


async def _push_dynamic_tools(ctx: Registration, previous_full: bool) -> None:
    from api.devices.live import device_tool_room, push_device_dynamic_tools_to_sid
    from api.devices.mcp_permissions import get_scope, reconcile_scope_with_capabilities, set_scope
    from api.services.device_tools import device_workspace_tools as workspace
    from connector_runtime.dispatch.desktop_device_tools import agent_endpoint_tools, device_type_of

    device_type = device_type_of(agents[ctx.sid])
    if ctx.user_id is None or device_type not in ("desktop", "browser", "android"):
        return
    await sio.enter_room(ctx.sid, device_tool_room(ctx.user_id, device_type))
    if device_type in ("desktop", "browser"):
        try:
            workspace.seed_defaults(ctx.user_id, device_type)
        except Exception:
            logger.exception("Failed to seed dynamic MCP tools: %s", ctx.device_id)
    await push_device_dynamic_tools_to_sid(ctx.user_id, device_type, ctx.sid)
    payload = workspace.device_payload(ctx.user_id, device_type)
    pushed = {
        str(tool.get("name") or "").strip()
        for tool in payload.get("tools") or []
        if str(tool.get("name") or "").strip()
    }
    full_caps = set(agent_endpoint_tools(agents[ctx.sid]) or []) | pushed
    if not full_caps:
        return
    if previous_full:
        set_scope(
            ctx.user_id, ctx.device_id, set(get_scope(ctx.user_id, ctx.device_id) or set()) | full_caps,
            ai_config_id=ctx.ai_config_id, device_type=device_type,
        )
    reconcile_scope_with_capabilities(
        ctx.user_id, ctx.device_id, sorted(full_caps),
        ai_config_id=ctx.ai_config_id, device_type=device_type,
    )


async def _resume_owned_work(ctx: Registration) -> None:
    from connector_runtime.dispatch.device_dispatch import resume_device_dispatch_queue

    try:
        await resume_device_dispatch_queue(ctx.device_id)
    except Exception:
        logger.exception("Failed to resume endpoint MCP queue: %s", ctx.device_id)
    if ctx.user_id is None:
        return
    try:
        from api.core.settings import settings
        from api.services.workflows.run_service import wake_offline_runs

        if settings.workflow_scheduler_enabled:
            with Session(engine) as session:
                wake_offline_runs(session, user_id=ctx.user_id, device_id=ctx.device_id)
    except Exception:
        logger.exception("Failed to wake offline workflows: %s", ctx.device_id)
    await emit_agent_list_for_user(ctx.user_id)


async def _push_pending_confirmations(ctx: Registration) -> None:
    """Backfill durable approvals that may have been created while this phone was offline."""
    if ctx.user_id is None:
        return
    from api.services.workflows.confirmation_notifications import notification_room, pending_notifications

    await sio.enter_room(ctx.sid, notification_room(ctx.user_id))
    with Session(engine) as session:
        items = pending_notifications(session, user_id=ctx.user_id)
    await sio.emit("workflow:confirmation_snapshot", {"items": items}, to=ctx.sid)


async def _push_pending_user_notifications(ctx: Registration) -> None:
    """Backfill unread app-fallback messages after an Android reconnect."""
    if ctx.user_id is None:
        return
    from api.services.notifications.user_notifications import (
        notification_room,
        pending_device_notifications,
    )

    await sio.enter_room(ctx.sid, notification_room(ctx.user_id))
    with Session(engine) as session:
        items = pending_device_notifications(session, user_id=ctx.user_id)
    await sio.emit("user:notification_snapshot", {"items": items}, to=ctx.sid)


async def handle_agent_register(sid: str, raw_info: object) -> None:
    try:
        info = validated_payload(AgentRegistrationPayload, raw_info)
    except ValidationError:
        logger.info("Agent registration rejected (invalid payload): sid=%s", sid)
        await sio.emit(
            "device:register_rejected",
            {"reason": "invalid registration payload", "error_code": "AGENT_PAYLOAD_INVALID"},
            to=sid,
        )
        return
    ctx = await _registration(sid, info)
    if ctx is None:
        return
    _store_live_agent(ctx)
    logger.info("Agent registered: %s user=%s ai=%s", ctx.device_id, ctx.user_id, ctx.ai_config_id)
    try:
        previous_full = _record_presence(ctx)
    except Exception:
        previous_full = False
        logger.exception("Failed to record endpoint agent presence: %s", ctx.device_id)
    await sio.emit("device:registered", {"id": ctx.device_id, "aiConfigId": ctx.ai_config_id}, to=sid)
    try:
        await _push_pending_confirmations(ctx)
    except Exception:
        logger.exception("Failed to backfill workflow confirmations: %s", ctx.device_id)
    try:
        await _push_pending_user_notifications(ctx)
    except Exception:
        logger.exception("Failed to backfill user notifications: %s", ctx.device_id)
    try:
        await _push_dynamic_tools(ctx, previous_full)
    except Exception:
        logger.exception("Failed to push dynamic MCP tools to device: %s", ctx.device_id)
    await _resume_owned_work(ctx)
