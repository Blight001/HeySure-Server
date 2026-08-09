"""Assemble Connector-owned endpoint Agent Socket.IO events."""

from api.sio import sio
from connector_runtime.socket_handlers import disconnect, registration, remote, tasks


def register_agent_socket_events() -> None:
    sio.on("device:register")(registration.handle_agent_register)
    sio.on("task:progress")(tasks.progress)
    sio.on("task:result")(tasks.result)
    sio.on("task:error")(tasks.error)
    sio.on("disconnect")(disconnect.handle_disconnect)

    @sio.on("flow:log")
    async def flow_log(_sid, data):
        await sio.emit("flow:monitor", data)

    @sio.on("rc:start")
    async def rc_start(sid, data):
        await remote.control_start(sid, data)

    for event in ("rc:offer", "rc:answer", "rc:ice", "rc:ready", "rc:error", "rc:stop", "rc:stopped"):
        async def control_relay(sid, data, event_name=event):
            await remote.control_relay(sid, event_name, data)

        sio.on(event)(control_relay)

    @sio.on("rt:open")
    async def rt_open(sid, data):
        await remote.terminal_open(sid, data)

    for event in ("rt:input", "rt:resize", "rt:data", "rt:exit", "rt:error", "rt:close"):
        async def terminal_relay(sid, data, event_name=event):
            await remote.terminal_relay(sid, event_name, data)

        sio.on(event)(terminal_relay)
