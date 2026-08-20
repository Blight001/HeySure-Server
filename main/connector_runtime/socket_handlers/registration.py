"""Authenticated, idempotent endpoint-device registration."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from sqlmodel import Session, select
from pydantic import ValidationError

from api.database import engine
from api.devices.catalog import DeviceCatalogError, PreparedDeviceCatalog, prepare_device_catalog
from api.devices.bindings import get_bindings
from api.devices.live import emit_agent_list_for_user
from api.models import AssistantAIConfig
from api.sio import agents, is_agent_shared_secret, resolve_agent_user, sio
from connector_runtime.socket_handlers.schemas import AgentRegistrationPayload, validated_payload


logger = logging.getLogger(__name__)
RETIRED_AGENT_PLATFORMS = frozenset({"heysure-cli-adapter"})


@dataclass
class Registration:
    sid: str
    info: Dict[str, Any]
    device_id: str
    user_id: Optional[int]
    account: Optional[str]
    ai_config_id: Optional[int]
    ai_config_ids: Tuple[int, ...]
    catalog: Optional[PreparedDeviceCatalog] = None


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


async def _reject_retired_platform(sid: str, info: Dict[str, Any]) -> bool:
    platform = str(info.get("platform") or "").strip().lower()
    if platform not in RETIRED_AGENT_PLATFORMS:
        return False
    logger.info("Retired agent platform rejected: platform=%s sid=%s", platform, sid)
    await sio.emit(
        "device:register_rejected",
        {
            "reason": "this device platform has been retired",
            "error_code": "AGENT_PLATFORM_RETIRED",
        },
        to=sid,
    )
    await sio.disconnect(sid)
    return True


async def _registration(sid: str, info: Dict[str, Any]) -> Optional[Registration]:
    accepted, user_id, account = await _authenticate(sid, info)
    if not accepted:
        return None
    device_id = str(info.get("id") or sid)
    bound_ais = tuple(get_bindings(user_id, device_id)) if user_id is not None else ()
    if user_id is not None and any(not _ai_belongs_to_user(value, user_id) for value in bound_ais):
        logger.warning(
            "Agent registration rejected (AI ownership mismatch): agent=%s user=%s ai=%s",
            info.get("id"), user_id, bound_ais,
        )
        await sio.emit(
            "device:register_rejected",
            {"reason": "selected AI does not belong to the logged-in user"},
            to=sid,
        )
        return None
    primary = bound_ais[0] if bound_ais else None
    return Registration(sid, info, device_id, user_id, account, primary, bound_ais)


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
        "boundAiConfigIds": list(ctx.ai_config_ids),
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


def _prepare_callable_catalog(ctx: Registration):
    from connector_runtime.dispatch.desktop_device_tools import (
        agent_endpoint_tool_defs,
        agent_endpoint_tools,
        device_type_of,
    )

    agent = {**ctx.info, "id": ctx.device_id, "userId": ctx.user_id}
    device_type = device_type_of(agent)
    if not device_type:
        raise DeviceCatalogError("DEVICE_TYPE_UNSUPPORTED", "device type could not be determined")
    agent["deviceType"] = device_type
    capabilities = sorted(agent_endpoint_tools(agent))
    definitions = agent_endpoint_tool_defs(agent)
    catalog = prepare_device_catalog({
        "capabilities": capabilities,
        "toolDefs": [{"name": name, **spec} for name, spec in definitions.items()],
        "aiDescription": ctx.info.get("aiDescription"),
        "catalogGeneration": ctx.info.get("catalogGeneration"),
        "catalogProtocolVersion": ctx.info.get("catalogProtocolVersion", 1),
    })
    return agent, device_type, capabilities, catalog


def _publish_committed_catalog(ctx: Registration, catalog: PreparedDeviceCatalog, committed: dict) -> None:
    # The committed catalog contains callable MCP tools only. Keep transport
    # capabilities from the original registration in the live socket record:
    # remote-control/terminal session gates read them from ``agents`` and must
    # not mistake catalog filtering for a device that lacks remote support.
    from api.devices.presence import NON_MCP_CAPABILITIES

    transports = {
        str(capability or "").strip()
        for capability in ctx.info.get("capabilities") or []
        if str(capability or "").strip() in NON_MCP_CAPABILITIES
    }
    ctx.info["capabilities"] = sorted(set(catalog.capabilities) | transports)
    ctx.info["toolDefs"] = list(catalog.tool_defs)
    ctx.info["aiDescription"] = catalog.reported_ai_description
    ctx.info["catalogGeneration"] = committed["catalog_generation"]
    ctx.info["catalogHash"] = committed["catalog_hash"]
    ctx.info["catalogProtocolVersion"] = committed["catalog_protocol_version"]


def _record_presence(ctx: Registration) -> tuple[Dict[Optional[int], bool], dict]:
    from api.devices.mcp_permissions import (
        get_scope,
        reconcile_saved_scope_for_capability_change,
        reconcile_scope_with_capabilities,
        saved_scope_was_full,
        set_scope,
    )
    from api.devices.presence import capabilities_for_device
    from api.devices.presence_catalog_store import PresenceCatalogUpdate, swap_presence_catalog
    agent, device_type, capabilities, accepted_catalog = _prepare_callable_catalog(ctx)
    ctx.catalog = accepted_catalog
    previous_full: Dict[Optional[int], bool] = {}
    expanded_scopes: Dict[Optional[int], set[str]] = {}
    if ctx.user_id is not None:
        previous = capabilities_for_device(ctx.user_id, ctx.device_id)
        default_saved = get_scope(
            ctx.user_id, ctx.device_id, None, fallback_to_default=False
        )
        for config_id in (None, *ctx.ai_config_ids):
            saved = get_scope(
                ctx.user_id, ctx.device_id, config_id, fallback_to_default=False
            )
            inherited = default_saved if config_id is not None and saved is None else saved
            previous_full[config_id] = saved_scope_was_full(inherited, previous)
            if saved is not None:
                expanded = reconcile_saved_scope_for_capability_change(saved, set(capabilities), previous)
                if expanded != saved:
                    expanded_scopes[config_id] = expanded
    committed = swap_presence_catalog(PresenceCatalogUpdate(
        user_id=ctx.user_id,
        device_id=ctx.device_id,
        ai_config_id=ctx.ai_config_id,
        device_type=device_type,
        capabilities=accepted_catalog.capabilities,
        tool_defs=accepted_catalog.tool_defs_map,
        name=agent.get("name"),
        platform=agent.get("platform"),
        icon=agent.get("icon") or "",
        reported_ai_description=accepted_catalog.reported_ai_description,
        catalog_hash=accepted_catalog.catalog_hash,
        requested_catalog_generation=accepted_catalog.requested_generation,
        catalog_protocol_version=accepted_catalog.protocol_version,
    ))
    _publish_committed_catalog(ctx, accepted_catalog, committed)
    if ctx.user_id is not None:
        try:
            for config_id, expanded in expanded_scopes.items():
                set_scope(
                    ctx.user_id, ctx.device_id, expanded,
                    ai_config_id=config_id, device_type=device_type,
                )
            for config_id in (None, *ctx.ai_config_ids):
                reconcile_scope_with_capabilities(
                    ctx.user_id, ctx.device_id, capabilities,
                    ai_config_id=config_id, device_type=device_type,
                )
        except Exception as exc:
            from api.devices.presence import set_offline

            set_offline(ctx.device_id)
            raise DeviceCatalogError(
                "DEVICE_SCOPE_RECONCILE_FAILED", "device permissions could not be reconciled"
            ) from exc
    return previous_full, committed


async def _push_dynamic_tools(ctx: Registration, previous_full: Dict[Optional[int], bool]) -> None:
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
    for config_id in (None, *ctx.ai_config_ids):
        if previous_full.get(config_id):
            saved = get_scope(
                ctx.user_id, ctx.device_id, config_id, fallback_to_default=False
            )
            set_scope(
                ctx.user_id, ctx.device_id, set(saved or set()) | full_caps,
                ai_config_id=config_id, device_type=device_type,
            )
        reconcile_scope_with_capabilities(
            ctx.user_id, ctx.device_id, sorted(full_caps),
            ai_config_id=config_id, device_type=device_type,
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
    if await _reject_retired_platform(sid, info):
        return
    ctx = await _registration(sid, info)
    if ctx is None:
        return
    try:
        # Validate the complete incoming generation before any live registry or
        # persisted state is mutated. _record_presence validates the
        # device-type-filtered callable generation once more before swapping it.
        prepare_device_catalog(info)
        previous_full, committed = _record_presence(ctx)
    except DeviceCatalogError as exc:
        logger.info("Agent catalog rejected: device=%s code=%s", ctx.device_id, exc.code)
        await sio.emit(
            "device:register_rejected",
            {"reason": str(exc), "error_code": exc.code},
            to=sid,
        )
        return
    except Exception:
        logger.exception("Failed to record endpoint agent presence: %s", ctx.device_id)
        await sio.emit(
            "device:register_rejected",
            {"reason": "failed to persist device catalog", "error_code": "DEVICE_CATALOG_PERSIST_FAILED"},
            to=sid,
        )
        return
    _store_live_agent(ctx)
    logger.info(
        "Agent registered: %s user=%s ai=%s catalog_generation=%s",
        ctx.device_id, ctx.user_id, ctx.ai_config_id, committed["catalog_generation"],
    )
    await sio.emit(
        "device:registered",
        {
            "id": ctx.device_id,
            "aiConfigId": ctx.ai_config_id,
            "boundAiConfigIds": list(ctx.ai_config_ids),
            "catalogGeneration": committed["catalog_generation"],
            "catalogHash": committed["catalog_hash"],
            "catalogProtocolVersion": committed["catalog_protocol_version"],
        },
        to=sid,
    )
    try:
        await _push_pending_user_notifications(ctx)
    except Exception:
        logger.exception("Failed to backfill user notifications: %s", ctx.device_id)
    try:
        await _push_dynamic_tools(ctx, previous_full)
    except Exception:
        logger.exception("Failed to push dynamic MCP tools to device: %s", ctx.device_id)
    await _resume_owned_work(ctx)
