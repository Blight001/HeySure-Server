"""Explicit, non-revivable state machine for external-controller runs."""

import time
from typing import Optional

from api.models.external_control import ExternalControllerRun, ExternalControllerTurn


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


TERMINAL_TURN_STATES = {"succeeded", "failed", "cancelled"}
TURN_TRANSITIONS = {
    "queued": {"running", "cancelled"},
    "running": {"queued", *TERMINAL_TURN_STATES},
    "succeeded": set(),
    "failed": set(),
    "cancelled": set(),
}


class TurnTransitionError(ValueError):
    pass


def transition_turn(
    row: ExternalControllerTurn,
    next_state: str,
    now: Optional[float] = None,
) -> None:
    """Move a conversation turn, allowing only lease-expiry recovery to queued."""
    target = str(next_state or "").strip().lower()
    if target not in TURN_TRANSITIONS.get(row.status, set()):
        raise TurnTransitionError(f"invalid turn transition: {row.status} -> {target}")
    current = now or time.time()
    row.status = target
    row.updated_at = current
    if target == "running" and row.started_at is None:
        row.started_at = current
    if target == "queued":
        row.lease_owner = ""
        row.lease_expires_at = None
    if target in TERMINAL_TURN_STATES:
        row.finished_at = current
        row.lease_expires_at = None
