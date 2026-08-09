"""Explicit Connector dispatch states and legal transitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class DispatchStatus(str, Enum):
    QUEUED = "queued"
    PENDING = "pending"
    COMPLETED = "completed"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class DispatchRecord:
    task_id: str
    user_id: int
    ai_config_id: Optional[int]
    ai_kind: str
    session_id: str
    session_name: Optional[str]
    device_id: str
    tool: str
    instruction: str
    args: Dict[str, Any] = field(default_factory=dict)
    suppress_session_message: bool = False


TERMINAL_DISPATCH_STATUSES = frozenset(
    {DispatchStatus.COMPLETED, DispatchStatus.ERROR, DispatchStatus.TIMEOUT, DispatchStatus.CANCELLED}
)

LEGAL_DISPATCH_TRANSITIONS = {
    DispatchStatus.QUEUED: frozenset({DispatchStatus.PENDING, DispatchStatus.TIMEOUT, DispatchStatus.CANCELLED}),
    DispatchStatus.PENDING: frozenset(
        {DispatchStatus.COMPLETED, DispatchStatus.ERROR, DispatchStatus.TIMEOUT, DispatchStatus.CANCELLED}
    ),
    DispatchStatus.COMPLETED: frozenset(),
    DispatchStatus.ERROR: frozenset(),
    DispatchStatus.TIMEOUT: frozenset(),
    DispatchStatus.CANCELLED: frozenset(),
}


def can_transition(current: str, target: str) -> bool:
    try:
        current_state = DispatchStatus(current)
        target_state = DispatchStatus(target)
    except ValueError:
        return False
    return current_state == target_state or target_state in LEGAL_DISPATCH_TRANSITIONS[current_state]


def require_transition(current: str, target: str) -> None:
    if not can_transition(current, target):
        raise ValueError(f"illegal dispatch transition: {current} -> {target}")
