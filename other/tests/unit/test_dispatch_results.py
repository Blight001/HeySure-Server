import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from connector_runtime.dispatch import dispatch_results


def test_result_persists_releases_waiter_emits_and_resumes(monkeypatch):
    envelope = dispatch_results.ResultEnvelope(
        task_id="task-a",
        context={"session_id": "session-a", "ai_config_id": 3, "ai_kind": "assistant"},
        device_id="device-a",
        success=True,
        tool="demo",
        summary="done",
        result={"value": 7},
        screenshot=False,
    )
    persisted = []
    applied = []
    released = []
    emitted = AsyncMock()
    completed = AsyncMock()
    monkeypatch.setattr(dispatch_results, "_recorded_outcome", lambda task_id: False)
    monkeypatch.setattr(dispatch_results, "_prepare_result", lambda data: envelope)
    monkeypatch.setattr(dispatch_results, "_persist_result", persisted.append)
    monkeypatch.setattr(
        dispatch_results,
        "_apply_workflow_result",
        lambda *args: applied.append(args),
    )
    monkeypatch.setattr(
        dispatch_results,
        "_release_waiter",
        lambda *args: released.append(args),
    )
    monkeypatch.setattr(dispatch_results, "_emit_to_user", emitted)
    monkeypatch.setattr(dispatch_results, "_complete_dispatch", completed)

    assert asyncio.run(dispatch_results.handle_task_result({"taskId": "task-a"}))

    assert persisted == [envelope]
    assert applied == [("task-a", True, {"value": 7}, None)]
    assert released[0][1]["summary"] == "done"
    emitted.assert_awaited_once()
    completed.assert_awaited_once_with("task-a", "device-a")


def test_duplicate_result_is_audited_without_side_effects(monkeypatch):
    audited = []
    monkeypatch.setattr(dispatch_results, "_recorded_outcome", lambda task_id: True)
    monkeypatch.setattr(
        dispatch_results,
        "_audit_duplicate",
        lambda *args: audited.append(args),
    )

    assert not asyncio.run(dispatch_results.handle_task_result({"taskId": "task-a"}))
    assert audited == [("task-a", "duplicate_terminal_result")]


def test_error_terminalizes_and_releases_dispatch(monkeypatch):
    state_updates = []
    finalized = []
    applied = []
    released = []
    emitted = AsyncMock()
    completed = AsyncMock()
    dispatch = SimpleNamespace(
        _device_kind_label=lambda device_id: "桌面端",
        _update_agent_task_state=lambda *args, **kwargs: state_updates.append((args, kwargs)),
    )
    monkeypatch.setattr(dispatch_results, "_recorded_outcome", lambda task_id: False)
    monkeypatch.setattr(dispatch_results, "_dispatch_module", lambda: dispatch)
    monkeypatch.setattr(
        dispatch_results,
        "_resolve_result_context",
        lambda data: {
            "device_id": "device-a",
            "tool": "demo",
            "suppress_session_message": True,
        },
    )
    monkeypatch.setattr(
        dispatch_results.repository,
        "finalize_dispatch_row",
        lambda *args, **kwargs: finalized.append((args, kwargs)),
    )
    monkeypatch.setattr(
        dispatch_results,
        "_apply_workflow_result",
        lambda *args: applied.append(args),
    )
    monkeypatch.setattr(
        dispatch_results,
        "_release_waiter",
        lambda *args: released.append(args),
    )
    monkeypatch.setattr(dispatch_results, "_emit_to_user", emitted)
    monkeypatch.setattr(dispatch_results, "_complete_dispatch", completed)

    assert asyncio.run(dispatch_results.handle_task_error({
        "taskId": "task-a",
        "error": "device failed",
    }))

    assert state_updates[0][1]["status"] == "error"
    assert finalized[0][1]["status"] == "error"
    assert applied == [("task-a", False, None, "device failed")]
    assert released[0][1]["error"] == "device failed"
    emitted.assert_awaited_once()
    completed.assert_awaited_once_with("task-a", "device-a")
