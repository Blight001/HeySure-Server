import asyncio
import base64
import time
from unittest.mock import AsyncMock

import pytest

from connector_runtime.dispatch import remote_terminal


def _run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture(autouse=True)
def clean_sessions(monkeypatch):
    remote_terminal._SESSIONS.clear()
    monkeypatch.setattr(remote_terminal, "_ensure_reaper", lambda: None)
    yield
    remote_terminal._SESSIONS.clear()


@pytest.fixture
def emitted(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(remote_terminal.sio, "emit", mock)
    return mock


def _allow_open(monkeypatch, *, user_id=7, owner=7, supported=True, device_sid="device-sid"):
    monkeypatch.setattr(remote_terminal, "_resolve_controller_user", lambda _token: user_id)
    monkeypatch.setattr(remote_terminal, "_find_device_sid", lambda _device_id: device_sid)
    monkeypatch.setattr(remote_terminal, "_agent_owner", lambda _sid: owner)
    monkeypatch.setattr(remote_terminal, "_agent_supports_rt", lambda _sid: supported)


def _open_payload(**overrides):
    return {
        "deviceId": "desktop-1",
        "token": "valid",
        "shell": "powershell",
        "cols": 120,
        "rows": 40,
        "cwd": "C:/workspace",
        **overrides,
    }


def _seed_session(**overrides):
    values = {
        "session_id": "rt_test",
        "device_id": "desktop-1",
        "user_id": 7,
        "controller_sid": "controller-sid",
        "device_sid": "device-sid",
    }
    values.update(overrides)
    session = remote_terminal.RtSession(**values)
    remote_terminal._SESSIONS[session.session_id] = session
    return session


@pytest.mark.parametrize(
    ("gate", "expected"),
    [
        ("unauthorized", "unauthorized"),
        ("offline", "offline"),
        ("forbidden", "forbidden"),
        ("unsupported", "unsupported"),
    ],
)
def test_open_session_enforces_auth_gates(monkeypatch, emitted, gate, expected):
    _allow_open(monkeypatch)
    if gate == "unauthorized":
        monkeypatch.setattr(remote_terminal, "_resolve_controller_user", lambda _token: None)
    elif gate == "offline":
        monkeypatch.setattr(remote_terminal, "_find_device_sid", lambda _device_id: None)
    elif gate == "forbidden":
        monkeypatch.setattr(remote_terminal, "_agent_owner", lambda _sid: 8)
    else:
        monkeypatch.setattr(remote_terminal, "_agent_supports_rt", lambda _sid: False)

    _run(remote_terminal.open_session("controller-sid", _open_payload()))

    assert remote_terminal._SESSIONS == {}
    assert emitted.await_args.args[0] == "rt:error"
    assert emitted.await_args.args[1]["code"] == expected
    assert emitted.await_args.kwargs["to"] == "controller-sid"


def test_open_session_normalizes_and_forwards_request(monkeypatch, emitted):
    _allow_open(monkeypatch)

    _run(remote_terminal.open_session("controller-sid", _open_payload(cols="100", rows="30")))

    assert len(remote_terminal._SESSIONS) == 1
    device_call, controller_call = emitted.await_args_list
    assert device_call.args[0] == "rt:open"
    assert device_call.args[1]["cols"] == 100
    assert device_call.args[1]["rows"] == 30
    assert device_call.kwargs["to"] == "device-sid"
    assert controller_call.args[0] == "rt:opened"
    assert controller_call.kwargs["to"] == "controller-sid"


@pytest.mark.parametrize(
    "overrides",
    [
        {"deviceId": ""},
        {"deviceId": "x" * (remote_terminal._MAX_DEVICE_ID_LENGTH + 1)},
        {"shell": "x" * (remote_terminal._MAX_SHELL_LENGTH + 1)},
        {"cwd": "x" * (remote_terminal._MAX_CWD_LENGTH + 1)},
        {"cols": remote_terminal._MAX_COLS + 1},
        {"rows": 0},
    ],
)
def test_open_session_rejects_out_of_bounds_payload(monkeypatch, emitted, overrides):
    _allow_open(monkeypatch)

    _run(remote_terminal.open_session("controller-sid", _open_payload(**overrides)))

    assert remote_terminal._SESSIONS == {}
    assert emitted.await_args.args[1]["code"] == "bad_request"


@pytest.mark.parametrize("limit_kind", ["user", "device"])
def test_open_session_enforces_concurrency_limits(monkeypatch, emitted, limit_kind):
    _allow_open(monkeypatch)
    count = (
        remote_terminal._MAX_SESSIONS_PER_USER
        if limit_kind == "user"
        else remote_terminal._MAX_SESSIONS_PER_DEVICE
    )
    for index in range(count):
        _seed_session(
            session_id=f"rt_existing_{index}",
            device_id="desktop-1" if limit_kind == "device" else f"desktop-{index + 2}",
            user_id=7,
            controller_sid=f"controller-{index}",
            device_sid=f"device-{index}",
        )

    _run(remote_terminal.open_session("controller-sid", _open_payload()))

    assert len(remote_terminal._SESSIONS) == count
    assert emitted.await_args.args[1]["code"] == "session_limit"


def test_relay_accepts_declared_directions_and_refreshes_activity(emitted):
    session = _seed_session()
    previous_activity = time.time() - 10
    session.last_activity = previous_activity
    encoded = base64.b64encode(b"echo ok\r").decode("ascii")

    _run(remote_terminal.relay("controller-sid", "rt:input", {"sessionId": "rt_test", "data": encoded}))

    assert emitted.await_args.args == ("rt:input", {"sessionId": "rt_test", "data": encoded})
    assert emitted.await_args.kwargs["to"] == "device-sid"
    assert session.last_activity > previous_activity

    emitted.reset_mock()
    _run(
        remote_terminal.relay(
            "device-sid",
            "rt:ready",
            {"sessionId": "rt_test", "shell": "powershell", "cols": 100, "rows": 30},
        )
    )
    assert emitted.await_args.args[0] == "rt:ready"
    assert emitted.await_args.kwargs["to"] == "controller-sid"


@pytest.mark.parametrize(
    ("sid", "event"),
    [("controller-sid", "rt:data"), ("device-sid", "rt:input")],
)
def test_relay_rejects_wrong_direction(emitted, sid, event):
    _seed_session()
    encoded = base64.b64encode(b"bytes").decode("ascii")

    _run(remote_terminal.relay(sid, event, {"sessionId": "rt_test", "data": encoded}))

    assert emitted.await_args.args[0] == "rt:error"
    assert emitted.await_args.args[1]["code"] == "invalid_direction"
    assert emitted.await_args.kwargs["to"] == sid
    assert "rt_test" in remote_terminal._SESSIONS


def test_relay_silently_drops_spoofed_sid(emitted):
    _seed_session()

    _run(remote_terminal.relay("attacker-sid", "rt:close", {"sessionId": "rt_test"}))

    emitted.assert_not_awaited()
    assert "rt_test" in remote_terminal._SESSIONS


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        ("rt:input", {"data": "not base64!"}),
        ("rt:data", {"data": "A" * (remote_terminal._MAX_BASE64_LENGTH + 4)}),
        ("rt:resize", {"cols": 0, "rows": 20}),
        ("rt:error", {"code": "x" * 65, "message": "failed"}),
    ],
)
def test_relay_rejects_malformed_or_oversized_payload(emitted, event, payload):
    _seed_session()
    sid = "controller-sid" if event in {"rt:input", "rt:resize"} else "device-sid"

    _run(remote_terminal.relay(sid, event, {"sessionId": "rt_test", **payload}))

    assert emitted.await_args.args[0] == "rt:error"
    assert emitted.await_args.args[1]["code"] == "bad_payload"
    assert "rt_test" in remote_terminal._SESSIONS


@pytest.mark.parametrize(
    ("sid", "expected_event", "target"),
    [
        ("controller-sid", "rt:close", "device-sid"),
        ("device-sid", "rt:exit", "controller-sid"),
    ],
)
def test_disconnect_notifies_survivor_and_cleans_session(emitted, sid, expected_event, target):
    _seed_session()

    _run(remote_terminal.handle_disconnect(sid))

    assert remote_terminal._SESSIONS == {}
    assert emitted.await_args.args[0] == expected_event
    assert emitted.await_args.kwargs["to"] == target


def test_purge_expired_uses_last_activity_and_notifies_both_peers(emitted):
    session = _seed_session()
    session.created_at = 1.0
    session.last_activity = 100.0
    now = 100.0 + remote_terminal._SESSION_TTL_SECONDS + 1

    expired = _run(remote_terminal._purge_expired(now=now))

    assert expired == 1
    assert remote_terminal._SESSIONS == {}
    assert [call.args[0] for call in emitted.await_args_list] == ["rt:close", "rt:exit"]
    assert emitted.await_args_list[0].kwargs["to"] == "device-sid"
    assert emitted.await_args_list[1].kwargs["to"] == "controller-sid"
    assert emitted.await_args_list[1].args[1]["reason"] == "idle_timeout"


def test_terminal_events_close_session(emitted):
    _seed_session()

    _run(
        remote_terminal.relay(
            "device-sid",
            "rt:exit",
            {"sessionId": "rt_test", "code": 0, "reason": "completed"},
        )
    )

    assert remote_terminal._SESSIONS == {}
    assert emitted.await_args.args[0] == "rt:exit"


def test_recent_activity_is_not_expired(emitted):
    session = _seed_session()
    session.created_at = 1.0
    session.last_activity = time.time()

    expired = _run(remote_terminal._purge_expired(now=session.last_activity + 1))

    assert expired == 0
    assert "rt_test" in remote_terminal._SESSIONS
    emitted.assert_not_awaited()
