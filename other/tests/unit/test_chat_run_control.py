from types import SimpleNamespace

from api.services.chat.chat_run_control import build_resumed_run, request_stop


def _row(status="running"):
    return SimpleNamespace(
        status=status,
        stop_requested=False,
        updated_at=0.0,
        started_at=None,
        finished_at=None,
        error_message=None,
    )


def test_request_stop_sets_flag_and_terminal_status():
    row = _row()

    assert request_stop(row, now=10.0)
    assert row.stop_requested is True
    assert row.status == "stopped"
    assert row.finished_at == 10.0


def test_request_stop_is_idempotent_for_terminal_rows():
    row = _row("completed")

    assert not request_stop(row, now=10.0)
    assert row.stop_requested is True
    assert row.status == "completed"


def test_build_resumed_run_preserves_context_but_requeues():
    previous = SimpleNamespace(
        user_id=7,
        ai_config_id=9,
        ai_kind="assistant",
        session_id="s1",
        session_name="会话",
        worker_kwargs_json='{"max_steps": 3}',
    )

    resumed = build_resumed_run(previous, "run-next")
    assert resumed.run_id == "run-next"
    assert resumed.status == "queued"
    assert resumed.stop_requested is False
    assert resumed.worker_kwargs_json == previous.worker_kwargs_json
