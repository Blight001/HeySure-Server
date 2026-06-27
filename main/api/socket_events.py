import logging
import time

from sqlmodel import Session, select

from api.devices.bindings import get_binding, set_binding
from api.devices.live import emit_agent_list_for_user
from api.database import engine
from api.models import AssistantAIConfig
from api.sio import (
    sio,
    agents,
    is_agent_shared_secret,
    resolve_agent_user,
)
from connector_runtime.dispatch.device_dispatch import (
    handle_task_error,
    handle_task_progress,
    handle_task_result,
    purge_stale_dispatches,
    resume_device_dispatch_queue,
)


logger = logging.getLogger(__name__)


def _ai_config_belongs_to_user(ai_config_id, user_id: int) -> bool:
    """Return True only if the AI config is owned by the registering user."""
    if ai_config_id in (None, "", 0):
        return True  # No AI claimed; allow generic registration.
    try:
        cfg_id = int(ai_config_id)
    except (TypeError, ValueError):
        return False
    with Session(engine) as session:
        cfg = session.exec(
            select(AssistantAIConfig).where(AssistantAIConfig.id == cfg_id)
        ).first()
        return bool(cfg and cfg.user_id == user_id)


def _coerce_positive_int(value):
    try:
        parsed = int(value)
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def _has_live_same_type_ai_binding(*, user_id: int, ai_config_id: int, device_id: str, agent_info: dict) -> bool:
    """Whether another live endpoint agent of the same type already owns this AI."""
    try:
        from connector_runtime.dispatch.desktop_device_tools import device_type_of
    except Exception:
        return False
    incoming_type = device_type_of(agent_info)
    if incoming_type not in {"desktop", "browser", "android", "workshop"}:
        return False
    target_cfg = _coerce_positive_int(ai_config_id)
    if not target_cfg:
        return False
    target_device_id = str(device_id or "").strip()
    for agent in agents.values():
        existing_id = str(agent.get("id") or "").strip()
        if not existing_id or existing_id == target_device_id:
            continue
        if _coerce_positive_int(agent.get("userId") or agent.get("user_id")) != user_id:
            continue
        if _coerce_positive_int(agent.get("aiConfigId") or agent.get("ai_config_id")) != target_cfg:
            continue
        if device_type_of(agent) == incoming_type:
            return True
    return False


def register_user_socket_events():
    """User-side handlers — what the browser/web client triggers.

    Currently only ``ui:join`` because user → server flows mostly go through
    REST. Browser ``connect`` / ``disconnect`` are still useful for visibility
    in logs even when no behavior depends on them.
    """
    @sio.on('connect')
    async def connect(sid, environ):
        logger.info('Client connected: %s', sid)

    @sio.on('ui:join')
    async def ui_join(sid, data):
        user_id = data.get("userId") if isinstance(data, dict) else None
        if user_id is None:
            return
        await sio.enter_room(sid, f"user_{user_id}")
        # Opportunistically clean up dispatches whose agent vanished.
        purge_stale_dispatches()
        await emit_agent_list_for_user(user_id, to=sid)


def register_agent_socket_events():
    """Agent-side handlers — what desktop / browser agents trigger.

    Lives on connector-runtime in split deployments so agent connections
    survive api-gateway restarts. The monolith registers BOTH this and the
    user-side block on the same sio instance.
    """
    @sio.on('device:register')
    async def agent_register(sid, info):
        info = info if isinstance(info, dict) else {}
        token = info.get('token')

        # Resolve to a logged-in user. Shared secret (if configured) is the
        # only allowed bypass and skips the per-user binding.
        owner_user_id = None
        owner_account = None
        if is_agent_shared_secret(token):
            owner_user_id = info.get('userId')
            try:
                owner_user_id = int(owner_user_id) if owner_user_id is not None else None
            except (TypeError, ValueError):
                owner_user_id = None
        else:
            resolved = resolve_agent_user(token)
            if not resolved:
                logger.info('Agent registration rejected (no auth): %s', info.get('id'))
                await sio.emit(
                    'device:register_rejected',
                    {'reason': 'agent must be logged in (invalid or missing user token)'},
                    to=sid,
                )
                return
            owner_user_id, owner_account = resolved

        # Devices no longer pick their own AI — they log in and connect, then an
        # operator assigns a server-side AI from the Workshop panel. Re-apply any
        # persisted binding for this (user, agent) so the assignment survives
        # reconnects. The persisted binding is the only source of truth: after an
        # operator clears the assignment, a reconnecting client must not revive a
        # stale locally-saved aiConfigId.
        device_id = str(info.get('id') or sid)
        bound_ai = get_binding(owner_user_id, device_id) if owner_user_id is not None else None
        claimed_ai = bound_ai
        if owner_user_id is not None and not _ai_config_belongs_to_user(claimed_ai, owner_user_id):
            logger.warning(
                f"Agent registration rejected (AI ownership mismatch): "
                f"agent={info.get('id')} user={owner_user_id} ai={claimed_ai}"
            )
            await sio.emit(
                'device:register_rejected',
                {'reason': 'selected AI does not belong to the logged-in user'},
                to=sid,
            )
            return
        if (
            owner_user_id is not None
            and claimed_ai
            and _has_live_same_type_ai_binding(
                user_id=owner_user_id,
                ai_config_id=claimed_ai,
                device_id=device_id,
                agent_info=info,
            )
        ):
            logger.warning(
                f"Agent persisted binding ignored (duplicate same-type AI binding): "
                f"agent={device_id} user={owner_user_id} ai={claimed_ai}"
            )
            set_binding(owner_user_id, device_id, None)
            claimed_ai = None

        # Idempotent: drop any stale socket entry for the same logical agent id
        # so a reconnect updates the socketId instead of duplicating the agent.
        for old_sid in [s for s, a in agents.items() if str(a.get('id')) == device_id and s != sid]:
            del agents[old_sid]

        info.pop('token', None)
        agents[sid] = {
            **info,
            'id': device_id,
            'aiConfigId': claimed_ai,
            'socketId': sid,
            'userId': owner_user_id,
            'userAccount': owner_account,
            'capabilities': info.get('capabilities') or [],
            'version': info.get('version') or '',
            'lifecycle': info.get('lifecycle') or 'registered',
            'connectedAt': time.time(),
            'lastSeenAt': time.time(),
            'lastTaskId': None,
            'lastTaskStatus': None,
            'lastTaskAt': None,
            'lastError': None,
            'source': 'socket',
            'dispatchable': True,
        }
        logger.info(
            f"Agent registered: {device_id} user={owner_user_id} "
            f"ai={agents[sid].get('aiConfigId')}"
        )
        # Mirror this endpoint agent into the shared DB presence snapshot so
        # ai-runtime / mcp-runtime (separate processes) can discover and
        # classify its tools. Never let a presence write break registration.
        try:
            from connector_runtime.dispatch.desktop_device_tools import (
                device_type_of,
                agent_endpoint_tools,
                agent_endpoint_tool_defs,
            )
            from api.devices.presence import upsert_presence
            from api.devices.mcp_permissions import reconcile_scope_with_capabilities

            atype = device_type_of(agents[sid])
            if atype:
                capabilities = sorted(agent_endpoint_tools(agents[sid]))
                upsert_presence(
                    owner_user_id,
                    device_id,
                    claimed_ai,
                    atype,
                    capabilities,
                    online=True,
                    tool_defs=agent_endpoint_tool_defs(agents[sid]),
                )
                if owner_user_id is not None:
                    reconcile_scope_with_capabilities(
                        owner_user_id,
                        device_id,
                        capabilities,
                        ai_config_id=claimed_ai,
                        device_type=atype,
                    )
        except Exception:
            logger.exception('Failed to record endpoint agent presence: %s', device_id)
        # Include the server-side bound AI so the device can show whether an AI
        # is assigned yet (status indicator: green = bound, yellow = none).
        await sio.emit('device:registered', {'id': device_id, 'aiConfigId': claimed_ai}, to=sid)
        # Push web-authored dynamic MCP tools for this device's type so a device
        # that was offline during an edit picks up the latest set on reconnect.
        # The device skips re-applying an unchanged revision, so this never loops
        # with the register it may trigger.
        try:
            from connector_runtime.dispatch.desktop_device_tools import (
                device_type_of,
                agent_endpoint_tool_defs,
            )
            from api.devices.live import push_device_dynamic_tools_to_sid
            from api.services.device_tools import device_workspace_tools as _dyn

            push_type = device_type_of(agents[sid])
            if owner_user_id is not None and push_type in ('desktop', 'browser', 'android'):
                # Join the device room so future edits (from any process) can push
                # via the socket relay, not just the gateway-local agents map.
                from api.devices.live import device_tool_room
                await sio.enter_room(sid, device_tool_room(owner_user_id, push_type))
                # Tools live as files in the user's workspace (not the DB). Seed the
                # factory-default desktop python tools on first connect (idempotent;
                # migrates any legacy DB rows to files once), then push the set down.
                try:
                    if push_type in ('desktop', 'browser'):
                        _dyn.seed_defaults(owner_user_id, push_type)
                except Exception:
                    logger.exception('Failed to seed dynamic MCP tools: %s', device_id)
                await push_device_dynamic_tools_to_sid(owner_user_id, push_type, sid)
        except Exception:
            logger.exception('Failed to push dynamic MCP tools to device: %s', device_id)
        try:
            await resume_device_dispatch_queue(device_id)
        except Exception:
            logger.exception('Failed to resume endpoint MCP queue: %s', device_id)
        if owner_user_id is not None:
            await emit_agent_list_for_user(owner_user_id)

    @sio.on('flow:log')
    async def flow_log(sid, data):
        await sio.emit('flow:monitor', data)

    @sio.on('task:progress')
    async def task_progress(sid, data):
        await handle_task_progress(data if isinstance(data, dict) else {})

    @sio.on('task:result')
    async def task_result(sid, data):
        await handle_task_result(data if isinstance(data, dict) else {})
        owner_user_id = (agents.get(sid) or {}).get('userId')
        if owner_user_id is not None:
            await emit_agent_list_for_user(owner_user_id)

    @sio.on('task:error')
    async def task_error(sid, data):
        await handle_task_error(data if isinstance(data, dict) else {})
        owner_user_id = (agents.get(sid) or {}).get('userId')
        if owner_user_id is not None:
            await emit_agent_list_for_user(owner_user_id)

    @sio.on('disconnect')
    async def disconnect(sid):
        if sid in agents:
            device_id_for_presence = str(agents[sid].get('id') or '')
            owner_user_id = agents[sid].get('userId')
            del agents[sid]
            try:
                from api.devices.presence import set_offline
                set_offline(device_id_for_presence)
            except Exception:
                logger.exception('Failed to mark endpoint agent offline: %s', device_id_for_presence)
            if owner_user_id is not None:
                await emit_agent_list_for_user(owner_user_id)


def register_socket_events():
    """Register both halves. Used by the monolith and (for safety) when
    the deployment is intentionally collapsed into one process."""
    register_user_socket_events()
    register_agent_socket_events()
