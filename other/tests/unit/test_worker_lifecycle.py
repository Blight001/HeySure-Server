from ai_runtime.inference import worker_lifecycle
from ai_runtime.inference.run_request import WorkerRequest


def _request():
    return WorkerRequest.create(
        run_id="run-a",
        user_id=7,
        ai_config_id=3,
        ai_kind="assistant",
        session_id="session-a",
        session_name="任务",
    )


def test_worker_lifecycle_runs_cleanup_after_success(monkeypatch):
    events = []
    monkeypatch.setattr(worker_lifecycle, "_heartbeat_loop", lambda *args: events.append("heartbeat"))
    monkeypatch.setattr(worker_lifecycle, "_start_qq_stream", lambda request: events.append("start"))
    monkeypatch.setattr(worker_lifecycle, "_finish_qq_stream", lambda request: events.append("finish"))
    monkeypatch.setattr(worker_lifecycle, "_resume_orphaned_injects", lambda request: events.append("resume"))

    worker_lifecycle.run_worker(
        _request(),
        lambda request: events.append(("run", request.run_id)),
    )

    assert "heartbeat" in events
    assert events[-3:] == [("run", "run-a"), "finish", "resume"]


def test_worker_lifecycle_runs_cleanup_after_failure(monkeypatch):
    events = []
    monkeypatch.setattr(worker_lifecycle, "_heartbeat_loop", lambda *args: None)
    monkeypatch.setattr(worker_lifecycle, "_start_qq_stream", lambda request: events.append("start"))
    monkeypatch.setattr(worker_lifecycle, "_finish_qq_stream", lambda request: events.append("finish"))
    monkeypatch.setattr(worker_lifecycle, "_resume_orphaned_injects", lambda request: events.append("resume"))

    try:
        worker_lifecycle.run_worker(
            _request(),
            lambda request: (_ for _ in ()).throw(RuntimeError("boom")),
        )
    except RuntimeError as error:
        assert str(error) == "boom"
    else:
        raise AssertionError("worker failure was swallowed")

    assert events == ["start", "finish", "resume"]
