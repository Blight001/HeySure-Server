"""Application-level ChatRun control operations.

Gateway routes keep authentication and response mapping here, while this
module owns the row mutations used by stop/resume.  Keeping these operations
next to the lifecycle state rules makes it harder for a new entry point to
forget the stop flag or create a non-queued resumed run.
"""

from __future__ import annotations

from typing import Optional

from api.chat_runtime.chat_run_state import set_status
from api.models import ChatRun


def request_stop(row: ChatRun, *, now: Optional[float] = None) -> bool:
    """Mark a run stopped and return whether its lifecycle changed."""

    row.stop_requested = True
    if str(row.status or "").strip() not in {"queued", "running"}:
        return False
    return set_status(row, "stopped", now=now, finished=True)


def build_resumed_run(previous: ChatRun, run_id: str) -> ChatRun:
    """Create the queued successor for a stopped run."""

    return ChatRun(
        run_id=run_id,
        user_id=previous.user_id,
        ai_config_id=previous.ai_config_id,
        ai_kind=previous.ai_kind,
        session_id=previous.session_id,
        session_name=previous.session_name,
        status="queued",
        stop_requested=False,
        worker_kwargs_json=previous.worker_kwargs_json,
    )
