import asyncio
from unittest.mock import AsyncMock

import pytest

from connector_runtime.dispatch import remote_control


@pytest.mark.parametrize(
    ("requested", "expected"),
    [("smooth", "smooth"), ("clear", "clear"), ("unexpected", "balanced")],
)
def test_start_session_forwards_sanitized_quality_preset(monkeypatch, requested, expected):
    emitted = AsyncMock()
    monkeypatch.setattr(remote_control.sio, "emit", emitted)
    monkeypatch.setattr(remote_control, "_resolve_controller_user", lambda _token: 7)
    monkeypatch.setattr(remote_control, "_find_device_sid", lambda _device_id: "device-sid")
    monkeypatch.setattr(remote_control, "_agent_owner", lambda _sid: 7)
    monkeypatch.setattr(remote_control, "_agent_supports_rc", lambda _sid: True)
    remote_control._SESSIONS.clear()

    asyncio.run(
        remote_control.start_session(
            "controller-sid",
            {"deviceId": "desktop-1", "token": "valid", "qualityPreset": requested},
        )
    )

    device_call = emitted.await_args_list[0]
    assert device_call.args[0] == "rc:start"
    assert device_call.args[1]["qualityPreset"] == expected
    assert device_call.kwargs["to"] == "device-sid"
    remote_control._SESSIONS.clear()


@pytest.mark.parametrize("disconnected_sid", ["controller-sid", "device-sid"])
def test_disconnect_cleans_session_for_either_peer(monkeypatch, disconnected_sid):
    emitted = AsyncMock()
    monkeypatch.setattr(remote_control.sio, "emit", emitted)
    remote_control._SESSIONS.clear()
    remote_control._SESSIONS["rc_test"] = remote_control.RcSession(
        session_id="rc_test",
        device_id="desktop-1",
        user_id=7,
        controller_sid="controller-sid",
        device_sid="device-sid",
    )

    asyncio.run(remote_control.handle_disconnect(disconnected_sid))

    assert "rc_test" not in remote_control._SESSIONS
    expected_event = "rc:stop" if disconnected_sid == "controller-sid" else "rc:stopped"
    assert emitted.await_args.args[0] == expected_event
