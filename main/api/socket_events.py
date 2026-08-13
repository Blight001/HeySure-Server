"""Gateway-owned browser-user Socket.IO event assembly."""

import asyncio
import logging
import time
from typing import Any

from socketio.exceptions import ConnectionRefusedError

from api.devices.live import emit_agent_list_for_user
from api.auth import decode_access_token
from api.sio import resolve_user_token, sio


logger = logging.getLogger(__name__)
_socket_loop = None
_expiry_tasks = {}


def _raw_token(value: Any) -> str:
    token = str(value or "").strip()
    return token.split(" ", 1)[1].strip() if token.startswith("Bearer ") else token


async def _disconnect_at_expiry(sid: str, expires_at: float) -> None:
    try:
        await asyncio.sleep(max(0.0, expires_at - time.time()))
        await sio.disconnect(sid)
    finally:
        _expiry_tasks.pop(sid, None)


async def _disconnect_user_sockets(user_id: int) -> None:
    participants = list(sio.manager.get_participants("/", f"user_{user_id}"))
    for participant in participants:
        sid = participant[0] if isinstance(participant, tuple) else participant
        await sio.disconnect(sid)


def disconnect_user_sockets(user_id: int) -> None:
    """Disconnect authenticated browser sockets after server-side revocation."""
    loop = _socket_loop
    if loop is None or loop.is_closed():
        return
    future = asyncio.run_coroutine_threadsafe(_disconnect_user_sockets(user_id), loop)
    future.add_done_callback(_log_disconnect_failure)


def _log_disconnect_failure(future) -> None:
    try:
        future.result()
    except Exception:
        logger.exception("Failed to disconnect revoked UI sockets")


def register_user_socket_events() -> None:
    @sio.on("connect")
    async def connect(sid, _environ, auth: Any = None):
        global _socket_loop
        token = auth.get("token") if isinstance(auth, dict) else None
        resolved = resolve_user_token(token or "")
        if resolved is None:
            logger.warning("Rejected unauthenticated UI socket: %s", sid)
            raise ConnectionRefusedError("authentication required")
        user_id, _account = resolved
        payload = decode_access_token(_raw_token(token)) or {}
        expires_at = float(payload.get("exp") or 0)
        if expires_at <= time.time():
            raise ConnectionRefusedError("authentication required")
        _socket_loop = asyncio.get_running_loop()
        await sio.save_session(sid, {"user_id": user_id})
        await sio.enter_room(sid, f"user_{user_id}")
        _expiry_tasks[sid] = asyncio.create_task(_disconnect_at_expiry(sid, expires_at))
        logger.info("Authenticated UI socket connected: %s", sid)

    @sio.on("disconnect")
    async def disconnect(sid):
        task = _expiry_tasks.pop(sid, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    @sio.on("ui:join")
    async def ui_join(sid, _data=None):
        try:
            socket_session = await sio.get_session(sid)
        except KeyError:
            return
        user_id = socket_session.get("user_id")
        if user_id is None:
            return
        await emit_agent_list_for_user(user_id, to=sid)


def register_socket_events() -> None:
    """Backward-compatible alias for Gateway user events."""
    register_user_socket_events()
