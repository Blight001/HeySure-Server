"""Gateway-owned browser-user Socket.IO event assembly."""

import logging

from api.devices.live import emit_agent_list_for_user
from api.sio import sio


logger = logging.getLogger(__name__)


def register_user_socket_events() -> None:
    @sio.on("connect")
    async def connect(sid, _environ):
        logger.info("Client connected: %s", sid)

    @sio.on("ui:join")
    async def ui_join(sid, data):
        user_id = data.get("userId") if isinstance(data, dict) else None
        if user_id is None:
            return
        await sio.enter_room(sid, f"user_{user_id}")
        await emit_agent_list_for_user(user_id, to=sid)


def register_socket_events() -> None:
    """Backward-compatible alias for Gateway user events."""
    register_user_socket_events()
