import asyncio
from unittest.mock import AsyncMock

from connector_runtime.socket_handlers import registration, tasks


def test_invalid_registration_payload_is_rejected(monkeypatch):
    emitted = AsyncMock()
    monkeypatch.setattr(registration.sio, "emit", emitted)

    asyncio.run(registration.handle_agent_register("bad-sid", {"capabilities": "not-a-list"}))

    payload = emitted.await_args.args[1]
    assert payload["error_code"] == "AGENT_PAYLOAD_INVALID"


def test_retired_cli_adapter_registration_is_rejected_and_disconnected(monkeypatch):
    emitted = AsyncMock()
    disconnected = AsyncMock()
    monkeypatch.setattr(registration.sio, "emit", emitted)
    monkeypatch.setattr(registration.sio, "disconnect", disconnected)

    asyncio.run(registration.handle_agent_register("retired-sid", {
        "id": "old-cli-adapter",
        "platform": "heysure-cli-adapter",
        "deviceType": "custom",
        "capabilities": ["cli.run"],
    }))

    payload = emitted.await_args.args[1]
    assert payload["error_code"] == "AGENT_PLATFORM_RETIRED"
    disconnected.assert_awaited_once_with("retired-sid")


def test_invalid_result_payload_has_stable_error_code():
    response = asyncio.run(tasks.result("sid", {"deviceId": "device-without-task"}))
    assert response == {"received": False, "error_code": "AGENT_TASK_PAYLOAD_INVALID"}


def test_valid_result_is_normalized_before_service_call(monkeypatch):
    handler = AsyncMock(return_value=True)
    monkeypatch.setattr(tasks, "handle_task_result", handler)
    monkeypatch.setattr(tasks, "emit_agent_list_for_user", AsyncMock())
    monkeypatch.setitem(tasks.agents, "sid", {"userId": 7})

    response = asyncio.run(
        tasks.result("sid", {"taskId": "task-7", "success": True, "extra": "retained"})
    )

    assert response == {"received": True, "taskId": "task-7", "duplicate": False}
    assert handler.await_args.args[0]["extra"] == "retained"
