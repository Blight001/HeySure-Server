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


def test_start_worker_run_publishes_initial_live_frame(monkeypatch):
    request = run_request.WorkerRequest.create(
        run_id="run-bot",
        user_id=7,
        ai_config_id=3,
        ai_kind="core",
        session_id="wechat_3_conn_peer",
        session_name="WeChat",
    )
    metadata = []
    phases = []
    monkeypatch.setattr(run_request, "_run_should_stop", lambda _run_id: False)
    monkeypatch.setattr(run_request, "_run_set_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(run_request, "_set_run_live_meta", lambda run_id, **values: metadata.append((run_id, values)))
    monkeypatch.setattr(run_request, "_set_run_live_phase", lambda run_id, phase: phases.append((run_id, phase)))
    monkeypatch.setattr(run_request, "ai_debug_stage", lambda *_args, **_kwargs: None)

    assert run_request.start_worker_run(request) is True
    assert metadata == [("run-bot", {
        "user_id": 7,
        "ai_config_id": 3,
        "ai_kind": "core",
        "session_id": "wechat_3_conn_peer",
        "session_name": "WeChat",
    })]
    assert phases == [("run-bot", "generating")]
