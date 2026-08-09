"""Idempotent Socket.IO disconnect cleanup."""

import logging

from api.devices.live import emit_agent_list_for_user
from api.sio import agents
from connector_runtime.dispatch import remote_control, remote_terminal


logger = logging.getLogger(__name__)


async def handle_disconnect(sid: str) -> None:
    for transport, label in ((remote_control, "control"), (remote_terminal, "terminal")):
        try:
            await transport.handle_disconnect(sid)
        except Exception:
            logger.exception("Failed to clean up remote-%s session: %s", label, sid)
    agent = agents.pop(sid, None)
    if not agent:
        return
    device_id = str(agent.get("id") or "")
    user_id = agent.get("userId")
    try:
        from api.devices.presence import set_offline

        set_offline(device_id)
    except Exception:
        logger.exception("Failed to mark endpoint agent offline: %s", device_id)
    if user_id is not None:
        await emit_agent_list_for_user(user_id)
