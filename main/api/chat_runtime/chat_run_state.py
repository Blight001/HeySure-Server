"""Explicit lifecycle rules for persisted :class:`ChatRun` rows.

ChatRun is shared by gateway, ai-runtime and connector processes.  Keeping the
allowed states in one small, dependency-free module prevents each process from
silently inventing a different terminal state or overwriting a finished run.
The helper deliberately mutates only lifecycle fields; callers remain
responsible for leases, stop flags and error payloads that belong to their
boundary.
"""

from __future__ import annotations

import time
from typing import Any, Optional


QUEUED = "queued"
RUNNING = "running"
COMPLETED = "completed"
ERROR = "error"
STOPPED = "stopped"

ACTIVE_STATUSES = frozenset({QUEUED, RUNNING})
TERMINAL_STATUSES = frozenset({COMPLETED, ERROR, STOPPED})
LEGACY_TERMINAL_STATUSES = frozenset({"failed"})
KNOWN_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES

# A requeue is intentional during worker restart/watchdog recovery.  A
# terminal row is never reopened; resume creates a new ChatRun instead.
ALLOWED_TRANSITIONS = {
    QUEUED: frozenset({RUNNING, ERROR, STOPPED}),
    RUNNING: frozenset({QUEUED, COMPLETED, ERROR, STOPPED}),
}


def is_active(status: object) -> bool:
    return str(status or "").strip() in ACTIVE_STATUSES


def is_terminal(status: object) -> bool:
    return str(status or "").strip() in TERMINAL_STATUSES | LEGACY_TERMINAL_STATUSES


def can_transition(current: object, target: object) -> bool:
    """Return whether a ChatRun may move from ``current`` to ``target``."""

    current_status = str(current or "").strip()
    target_status = str(target or "").strip()
    if current_status not in KNOWN_STATUSES or target_status not in KNOWN_STATUSES:
        return False
    if current_status == target_status:
        return current_status in ACTIVE_STATUSES
    return target_status in ALLOWED_TRANSITIONS.get(current_status, ())


def _validate_transition(current: str, target: str) -> bool:
    if target not in KNOWN_STATUSES:
        raise ValueError(f"unknown ChatRun status: {target!r}")
    if current in LEGACY_TERMINAL_STATUSES:
        return False
    if current not in KNOWN_STATUSES:
        raise ValueError(f"unknown current ChatRun status: {current!r}")
    if current == target:
        return current in ACTIVE_STATUSES
    return can_transition(current, target)


def set_status(
    row: Any,
    target: str,
    *,
    now: Optional[float] = None,
    error_message: Optional[str] = None,
    finished: bool = False,
) -> bool:
    """Apply one lifecycle transition and return whether it was accepted.

    Repeated updates to an active state are accepted so heartbeat/worker code
    can refresh ``updated_at``.  Repeated or late writes to a terminal state are
    rejected, making completion/error/stop notifications idempotent.
    ``ValueError`` signals a programming error (unknown state or illegal edge)
    rather than silently persisting a state the rest of the system cannot query.
    """

    current = str(getattr(row, "status", "") or "").strip()
    target_status = str(target or "").strip()
    if not _validate_transition(current, target_status):
        return False

    timestamp = time.time() if now is None else float(now)
    row.status = target_status
    row.updated_at = timestamp
    if target_status == RUNNING and getattr(row, "started_at", None) is None:
        row.started_at = timestamp
    if error_message is not None:
        row.error_message = error_message
    if finished or target_status in TERMINAL_STATUSES:
        row.finished_at = getattr(row, "finished_at", None) or timestamp
    return True
