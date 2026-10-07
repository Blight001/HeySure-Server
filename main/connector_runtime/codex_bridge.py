"""Socket relay for local Codex app-server sessions."""

from __future__ import annotations

from typing import Any

from api.sio import agents, sio


def _sid_for_device(device_id: str) -> str | None:
    for sid, agent in agents.items():
        if str(agent.get("id") or "") == str(device_id):
            return sid
    return None


async def send_to_device(*, device_id: str, event: str, payload: dict[str, Any]) -> dict[str, Any]:
    sid = _sid_for_device(device_id)
    if not sid:
        return {"ok": False, "error": "device_offline"}
    await sio.emit(event, payload, to=sid)
    return {"ok": True, "deviceId": device_id, "event": event}

