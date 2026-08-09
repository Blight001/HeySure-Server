"""Remote-control and remote-terminal relay adapters."""

from connector_runtime.dispatch import remote_control, remote_terminal


def payload(data: object) -> dict:
    return data if isinstance(data, dict) else {}


async def control_start(sid: str, data: object) -> None:
    await remote_control.start_session(sid, payload(data))


async def control_relay(sid: str, event: str, data: object) -> None:
    await remote_control.relay(sid, event, payload(data))


async def terminal_open(sid: str, data: object) -> None:
    await remote_terminal.open_session(sid, payload(data))


async def terminal_relay(sid: str, event: str, data: object) -> None:
    await remote_terminal.relay(sid, event, payload(data))
