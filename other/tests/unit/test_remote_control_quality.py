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


def test_start_session_forwards_only_supported_web_mirror_negotiation(monkeypatch):
    emitted = AsyncMock()
    monkeypatch.setattr(remote_control.sio, "emit", emitted)
    monkeypatch.setattr(remote_control, "_resolve_controller_user", lambda _token: 7)
    monkeypatch.setattr(remote_control, "_find_device_sid", lambda _device_id: "device-sid")
    monkeypatch.setattr(remote_control, "_agent_owner", lambda _sid: 7)
    monkeypatch.setattr(remote_control, "_agent_supports_rc", lambda _sid: True)
    monkeypatch.setattr(remote_control, "_agent_supports_web_mirror", lambda _sid: True)
    remote_control._SESSIONS.clear()

    asyncio.run(
        remote_control.start_session(
            "controller-sid",
            {
                "deviceId": "browser-1",
                "token": "valid",
                "requestedSurfaces": [" DOM ", "video", "dom", "html", 1],
                "protocolVersions": [True, "1", 2, 1, 1],
            },
        )
    )

    payload = emitted.await_args_list[0].args[1]
    assert payload["requestedSurfaces"] == ["dom", "video"]
    assert payload["protocolVersions"] == [1]
    assert set(payload) == {"sessionId", "qualityPreset", "requestedSurfaces", "protocolVersions"}
    remote_control._SESSIONS.clear()


def test_start_session_drops_dom_surface_without_device_capability(monkeypatch):
    emitted = AsyncMock()
    monkeypatch.setattr(remote_control.sio, "emit", emitted)
    monkeypatch.setattr(remote_control, "_resolve_controller_user", lambda _token: 7)
    monkeypatch.setattr(remote_control, "_find_device_sid", lambda _device_id: "device-sid")
    monkeypatch.setattr(remote_control, "_agent_owner", lambda _sid: 7)
    monkeypatch.setattr(remote_control, "_agent_supports_rc", lambda _sid: True)
    monkeypatch.setattr(remote_control, "_agent_supports_web_mirror", lambda _sid: False)
    remote_control._SESSIONS.clear()

    asyncio.run(
        remote_control.start_session(
            "controller-sid",
            {
                "deviceId": "browser-1",
                "token": "valid",
                "requestedSurfaces": ["dom", "video"],
                "protocolVersions": [1],
            },
        )
    )

    payload = emitted.await_args_list[0].args[1]
    assert payload["requestedSurfaces"] == ["video"]
    remote_control._SESSIONS.clear()


def test_start_session_rejects_second_operator_for_same_device(monkeypatch):
    emitted = AsyncMock()
    monkeypatch.setattr(remote_control.sio, "emit", emitted)
    monkeypatch.setattr(remote_control, "_resolve_controller_user", lambda _token: 7)
    monkeypatch.setattr(remote_control, "_find_device_sid", lambda _device_id: "device-sid")
    monkeypatch.setattr(remote_control, "_agent_owner", lambda _sid: 7)
    monkeypatch.setattr(remote_control, "_agent_supports_rc", lambda _sid: True)
    remote_control._SESSIONS.clear()
    remote_control._SESSIONS["rc_existing"] = remote_control.RcSession(
        session_id="rc_existing",
        device_id="browser-1",
        user_id=7,
        controller_sid="first-controller",
        device_sid="device-sid",
    )

    asyncio.run(
        remote_control.start_session(
            "second-controller",
            {"deviceId": "browser-1", "token": "valid"},
        )
    )

    assert set(remote_control._SESSIONS) == {"rc_existing"}
    assert emitted.await_count == 1
    assert emitted.await_args.args[0] == "rc:error"
    assert emitted.await_args.args[1]["code"] == "busy"
    assert emitted.await_args.kwargs["to"] == "second-controller"
    remote_control._SESSIONS.clear()


@pytest.mark.parametrize(
    ("requested_surfaces", "protocol_versions"),
    [
        (None, None),
        ("dom", 1),
        (["html", "canvas"], [2, 3]),
        (["invalid"] * 8 + ["dom"], [2] * 8 + [1]),
    ],
)
def test_start_session_omits_absent_malformed_or_out_of_bounds_negotiation(
    monkeypatch, requested_surfaces, protocol_versions,
):
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
            {
                "deviceId": "browser-1",
                "token": "valid",
                "requestedSurfaces": requested_surfaces,
                "protocolVersions": protocol_versions,
            },
        )
    )

    payload = emitted.await_args_list[0].args[1]
    assert "requestedSurfaces" not in payload
    assert "protocolVersions" not in payload
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
