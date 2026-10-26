from types import SimpleNamespace

import pytest

from api.chat_runtime.chat_run_state import (
    COMPLETED,
    ERROR,
    QUEUED,
    RUNNING,
    STOPPED,
    can_transition,
    set_status,
)


def _run(status: str = QUEUED):
    return SimpleNamespace(
        status=status,
        updated_at=0.0,
        started_at=None,
        finished_at=None,
        error_message=None,
    )


def test_chat_run_lifecycle_accepts_queue_run_complete():
    row = _run()

    assert set_status(row, RUNNING, now=10.0)
    assert row.status == RUNNING
    assert row.started_at == 10.0
    assert set_status(row, COMPLETED, now=20.0)
    assert row.finished_at == 20.0
    assert row.updated_at == 20.0


def test_terminal_chat_run_cannot_be_reopened_or_overwritten():
    row = _run(COMPLETED)

    assert not set_status(row, ERROR, now=30.0)
    assert not set_status(row, COMPLETED, now=31.0)
    assert row.status == COMPLETED
    assert row.updated_at == 0.0


def test_watchdog_requeue_and_stop_are_explicit_edges():
    assert can_transition(RUNNING, QUEUED)
    assert can_transition(QUEUED, STOPPED)
    assert not can_transition(COMPLETED, QUEUED)

    row = _run(RUNNING)
    assert set_status(row, QUEUED, now=40.0)
    assert row.finished_at is None
    assert set_status(row, STOPPED, now=50.0, finished=True)
    assert row.finished_at == 50.0


def test_unknown_or_illegal_status_is_not_silently_persisted():
    with pytest.raises(ValueError, match="unknown ChatRun status"):
        set_status(_run(), "failed")

    row = _run(QUEUED)
    assert not set_status(row, COMPLETED, now=60.0)
    assert row.status == QUEUED
