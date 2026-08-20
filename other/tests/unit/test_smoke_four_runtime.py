import asyncio

import pytest

from other.scripts import smoke_four_runtime


class FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_dispatch_wait_can_expire_silent_agent(monkeypatch):
    calls = []
    responses = iter([
        FakeResponse({"task_id": "task-a"}),
        FakeResponse({"expired": True}),
        FakeResponse({"status": "timeout", "success": False}),
    ])

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return next(responses)

    monkeypatch.setattr(smoke_four_runtime, "_request", request)

    payload = asyncio.run(smoke_four_runtime._dispatch_and_wait(
        "http://connector:3002",
        {"Authorization": "Bearer internal"},
        7,
        3,
        2.0,
        expire=True,
    ))

    assert payload == {"status": "timeout", "success": False}
    assert "/dispatch/expire/task-a" in calls[1][1]


def test_dispatch_wait_rejects_failed_expiration(monkeypatch):
    responses = iter([
        FakeResponse({"task_id": "task-a"}),
        FakeResponse({"expired": False}),
    ])
    monkeypatch.setattr(
        smoke_four_runtime,
        "_request",
        lambda *args, **kwargs: next(responses),
    )

    with pytest.raises(RuntimeError, match="was not expired"):
        asyncio.run(smoke_four_runtime._dispatch_and_wait(
            "http://connector:3002",
            {},
            7,
            3,
            2.0,
            expire=True,
        ))


def test_success_contract_checks_terminal_payload():
    smoke_four_runtime._assert_success({
        "status": "completed",
        "success": True,
        "result": {"echo": {"url": "https://example.test"}},
    })
    with pytest.raises(RuntimeError, match="simulated dispatch failed"):
        smoke_four_runtime._assert_success({
            "status": "error",
            "success": False,
            "result": None,
        })
    with pytest.raises(RuntimeError, match="unexpected dispatch result"):
        smoke_four_runtime._assert_success({
            "status": "completed",
            "success": True,
            "result": {"echo": {"url": "https://wrong.test"}},
        })


def test_cleanup_retries_until_simulated_device_is_offline(monkeypatch):
    calls = []
    responses = iter([
        FakeResponse({}, status_code=400, text="device is online"),
        FakeResponse({"ok": True}),
    ])

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return next(responses)

    monkeypatch.setattr(smoke_four_runtime, "_request", request)
    monkeypatch.setattr(smoke_four_runtime.time, "sleep", lambda _seconds: None)

    smoke_four_runtime._cleanup_simulated_device(
        "http://gateway:3000", "jwt", 1.0,
    )

    assert [call[0] for call in calls] == ["DELETE", "DELETE"]
    assert calls[-1][1].endswith("/api/devices/ci-simulated-browser")
    assert calls[-1][2]["headers"] == {"Authorization": "Bearer jwt"}


@pytest.mark.parametrize("round_trip_fails, expected", [(False, 0), (True, 1)])
def test_main_always_cleans_simulated_device(
    monkeypatch, round_trip_fails, expected,
):
    cleaned = []

    async def round_trip(*_args, **_kwargs):
        if round_trip_fails:
            raise RuntimeError("dispatch failed")

    monkeypatch.setattr(
        smoke_four_runtime.sys,
        "argv",
        ["smoke_four_runtime.py", "--internal-token", "internal"],
    )
    monkeypatch.setattr(smoke_four_runtime, "_wait_ready", lambda *_args: None)
    monkeypatch.setattr(
        smoke_four_runtime,
        "_login_or_register",
        lambda *_args: {"access_token": "jwt", "user": {"id": 7}},
    )
    monkeypatch.setattr(smoke_four_runtime, "_agent_round_trip", round_trip)
    monkeypatch.setattr(
        smoke_four_runtime,
        "_cleanup_simulated_device",
        lambda gateway, token, timeout: cleaned.append((gateway, token, timeout)),
    )

    assert smoke_four_runtime.main() == expected
    assert cleaned == [("http://127.0.0.1:3000", "jwt", 60.0)]
