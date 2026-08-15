"""Pure state-machine rules for long-running maintenance work orders."""

STATUSES = frozenset({
    "queued", "running", "waiting_user", "succeeded", "failed", "cancelled",
})
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
PHASES = (
    "triage", "diagnose", "plan", "implement", "test", "review", "commit",
    "push", "release", "verify",
)
_PHASE_INDEX = {value: index for index, value in enumerate(PHASES)}
_TRANSITIONS = {
    "queued": frozenset({"running", "failed", "cancelled"}),
    "running": frozenset({"waiting_user", "succeeded", "failed", "cancelled"}),
    "waiting_user": frozenset({"running", "failed", "cancelled"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


def validate_status_transition(current: str, target: str) -> None:
    if target not in STATUSES:
        raise ValueError(f"unknown maintenance status: {target}")
    if target == current:
        return
    if target not in _TRANSITIONS.get(current, frozenset()):
        raise ValueError(f"illegal maintenance status transition: {current} -> {target}")


def validate_phase_transition(current: str, target: str) -> None:
    if target not in _PHASE_INDEX:
        raise ValueError(f"unknown maintenance phase: {target}")
    if _PHASE_INDEX[target] < _PHASE_INDEX.get(current, 0):
        raise ValueError(f"maintenance phase cannot move backwards: {current} -> {target}")

