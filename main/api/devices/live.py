"""User-scoped snapshots for live endpoint agents and the builtin workshop."""

import logging

from api.sio import agents, sio


logger = logging.getLogger(__name__)


def _positive_int(value):
    try:
        parsed = int(value)
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def connected_agent_rows_for_user(user_id: int):
    """Build the live agent snapshot visible to one user."""
    uid = _positive_int(user_id)
    if uid is None:
        return []
    rows = [
        agent for agent in agents.values()
        if _positive_int(agent.get("userId") or agent.get("user_id")) == uid
    ]
    try:
        from tools import engine as toolbox_engine
        from library import engine as workshop_engine

        workshop_engine.ensure_presence_for_user(uid)
        rows.append(workshop_engine.connected_entry_for_user(uid))
        rows.append(toolbox_engine.toolbox_connected_entry_for_user(uid))
    except Exception:
        logger.exception("failed to add builtin workshop to agent snapshot user=%s", uid)
    return rows


async def emit_agent_list_for_user(user_id: int, *, to=None) -> None:
    """Emit a user-scoped snapshot to one socket or the user's UI room."""
    uid = _positive_int(user_id)
    if uid is None:
        return
    await sio.emit(
        "device:list",
        connected_agent_rows_for_user(uid),
        to=to or f"user_{uid}",
    )


def device_tool_room(user_id: int, device_type: str) -> str:
    """Room every online device of one (user, device_type) joins on register.

    Pushing to this room (instead of per-sid) lets ai-runtime / mcp-runtime emit
    via the socket relay — they have no access to the gateway-only ``agents``
    map — so an AI editing its own tools can sync devices from any process."""
    return f"device_{int(user_id)}_{device_type}"


def _online_device_count(user_id: int, device_type: str) -> int:
    from api.database import engine
    from api.models import DevicePresence
    from sqlmodel import Session, select

    uid = _positive_int(user_id)
    if uid is None:
        return 0
    with Session(engine) as session:
        rows = session.exec(
            select(DevicePresence).where(
                DevicePresence.user_id == uid,
                DevicePresence.device_type == device_type,
                DevicePresence.online == True,  # noqa: E712
            )
        ).all()
    return len({str(r.device_id) for r in rows if r.device_id})


async def push_device_dynamic_tools(user_id: int, device_type: str) -> int:
    """Push the current web/AI-authored dynamic MCP set to every online device of
    this type (via the device room). Returns the online device count. The device
    merges the set into its runtime and re-reports its catalog, so the rest of
    the endpoint pipeline keeps working unchanged."""
    from api.services.device_tools import device_workspace_tools as dyn

    try:
        dtype = dyn.normalize_device_type(device_type)
    except ValueError:
        return 0
    uid = _positive_int(user_id)
    if uid is None:
        return 0
    payload = dyn.device_payload(uid, dtype)
    await sio.emit("device:tool-config", payload, room=device_tool_room(uid, dtype))
    return _online_device_count(uid, dtype)


async def push_device_dynamic_tools_to_sid(user_id: int, device_type: str, sid: str) -> None:
    """Push the dynamic MCP set to one freshly-registered socket so it picks up
    edits made while it was offline."""
    from api.services.device_tools import device_workspace_tools as dyn

    try:
        dtype = dyn.normalize_device_type(device_type)
    except ValueError:
        return
    await sio.emit("device:tool-config", dyn.device_payload(user_id, dtype), to=sid)
