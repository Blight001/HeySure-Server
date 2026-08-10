"""Explicit, non-revivable state machine for external-controller runs."""

import time
from typing import Optional

from api.models.external_control import ExternalControllerRun


TERMINAL_RUN_STATES = {"succeeded", "failed", "cancelled", "expired"}
RUN_TRANSITIONS = {
    "queued": {"leased", "cancelled", "expired"},
    "leased": {"running", "failed", "cancelled", "expired"},
    "running": TERMINAL_RUN_STATES,
    "succeeded": set(),
    "failed": set(),
    "cancelled": set(),
    "expired": set(),
}


class RunTransitionError(ValueError):
    pass


def transition_run(
    row: ExternalControllerRun,
    next_state: str,
    now: Optional[float] = None,
) -> None:
    target = str(next_state or "").strip().lower()
    allowed = RUN_TRANSITIONS.get(row.status, set())
    if target not in allowed:
        raise RunTransitionError(f"invalid run transition: {row.status} -> {target}")
    current = now or time.time()
    row.status = target
    row.updated_at = current
    if target == "running" and row.started_at is None:
        row.started_at = current
    if target in TERMINAL_RUN_STATES:
        row.finished_at = current
        row.lease_expires_at = None
