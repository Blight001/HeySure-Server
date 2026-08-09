import asyncio

import pytest

from other.scripts import smoke_four_runtime


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = ""

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
