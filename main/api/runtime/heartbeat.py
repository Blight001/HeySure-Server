"""Worker heartbeat + watchdog reaper for ChatRun.

When a worker is alive it must call :func:`tick` periodically (≤ every few
seconds) on the run it is processing. The watchdog runs in api-gateway and
periodically scans for rows that claim ``status='running'`` but have not
been touched for longer than :data:`STALE_AFTER_SECONDS`. Those rows are
returned to ``queued`` so another ai-runtime instance can resume them from
persisted chat/MCP history after a process restart or crash.
"""

from __future__ import annotations

import time
from typing import List

from sqlmodel import Session, select

from ..database import engine
from ..models import ChatRun


# A worker must call ``tick`` at least every TICK_INTERVAL_SECONDS while a
# run is in flight. The watchdog allows STALE_AFTER_SECONDS of grace before
# considering the worker dead.
TICK_INTERVAL_SECONDS: float = 5.0
STALE_AFTER_SECONDS: float = 60.0


def tick(run_id: str) -> None:
    """Bump ``heartbeat_at`` for an active run. Safe to call frequently."""
    with Session(engine) as session:
        row = session.exec(select(ChatRun).where(ChatRun.run_id == run_id)).first()
        if not row or row.status != "running":
            return
        row.heartbeat_at = time.time()
        session.add(row)
        session.commit()


def reap_stale_runs() -> List[str]:
    """Return runs with a silent heartbeat to the durable queue.

    The inference loop persists assistant tool calls and MCP results as chat
    messages at every completed tool boundary. Re-running the same ``ChatRun``
    therefore rebuilds context from the last durable boundary instead of losing
    the conversation. A crash during an in-flight external side effect still
    cannot provide exactly-once semantics; graceful shutdown draining minimizes
    that narrow window.

    Returns the run IDs that were requeued.
    """
    now = time.time()
    threshold = now - STALE_AFTER_SECONDS
    reaped: List[str] = []
    with Session(engine) as session:
        rows = session.exec(
            select(ChatRun).where(ChatRun.status == "running")
        ).all()
        for row in rows:
            last = row.heartbeat_at or row.started_at or row.updated_at or row.created_at
            if last is None or last < threshold:
                row.status = "queued"
                row.error_message = None
                row.finished_at = None
                row.heartbeat_at = None
                row.updated_at = now
                session.add(row)
                reaped.append(row.run_id)
        if reaped:
            session.commit()
    return reaped
