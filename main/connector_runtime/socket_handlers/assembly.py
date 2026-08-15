"""Assemble Connector-owned endpoint Agent Socket.IO events."""

from api.sio import sio
from connector_runtime.socket_handlers import disconnect, registration, remote, tasks
from connector_runtime import maintenance


def register_agent_socket_events() -> None:
    sio.on("device:register")(registration.handle_agent_register)
    sio.on("task:progress")(tasks.progress)
    sio.on("task:result")(tasks.result)
    sio.on("task:error")(tasks.error)
    sio.on("disconnect")(disconnect.handle_disconnect)
    @sio.on("codex:run_started")
    async def codex_run_started(sid, data):
        return await maintenance.guarded(maintenance.run_started, sid, data)

    @sio.on("codex:event")
    async def codex_event(sid, data):
        return await maintenance.guarded(maintenance.event, sid, data)

    @sio.on("codex:approval_requested")
    async def codex_approval_requested(sid, data):
        return await maintenance.guarded(maintenance.approval_requested, sid, data)

    @sio.on("codex:run_completed")
    async def codex_run_completed(sid, data):
        return await maintenance.guarded(maintenance.run_completed, sid, data)

    @sio.on("codex:command_ack")
    async def codex_command_ack(sid, data):
        return await maintenance.guarded(maintenance.command_ack, sid, data)

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
