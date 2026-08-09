from dataclasses import FrozenInstanceError

import pytest

from ai_runtime.inference import run_request


def test_worker_request_freezes_selected_tool_snapshot():
    selected = {"workspace.read"}
    request = run_request.WorkerRequest.create(
        run_id="run-1",
        user_id=7,
        ai_config_id=8,
        ai_kind="assistant",
        session_id="session-1",
        session_name="Session",
        selected_mcp_tools=selected,
    )
    selected.add("workspace.write")

    assert request.selected_mcp_tools == frozenset({"workspace.read"})
    with pytest.raises(FrozenInstanceError):
        request.session_name = "changed"


def test_start_worker_run_stops_pre_cancelled_request(monkeypatch):
    request = run_request.WorkerRequest.create(
        run_id="run-stop",
        user_id=1,
        ai_config_id=None,
        ai_kind="assistant",
        session_id="session",
        session_name="Session",
    )
    statuses = []
    monkeypatch.setattr(run_request, "_run_should_stop", lambda _run_id: True)
    monkeypatch.setattr(
        run_request,
        "_run_set_status",
        lambda *args, **kwargs: statuses.append((args, kwargs)),
    )

    assert run_request.start_worker_run(request) is False
    assert statuses == [(('run-stop', 'stopped'), {'finished': True})]
