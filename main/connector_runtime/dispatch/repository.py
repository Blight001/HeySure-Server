"""Persistence and lease ownership for connector dispatch tasks."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, Optional

from sqlmodel import Session, select

from api.core.settings import settings
from api.database import engine
from api.models import AgentDispatchTask
from api.runtime.health import state_for
from connector_runtime.dispatch.models import DispatchRecord, TERMINAL_DISPATCH_STATUSES, require_transition


logger = logging.getLogger(__name__)
CONNECTOR_INSTANCE_ID = state_for("connector").instance_id
_DISPATCH_QUEUE_LOCK = threading.Lock()
_LEGACY_DISPATCH_TTL_SECONDS = 1800


def lease_deadline(now: Optional[float] = None) -> float:
    return (now or time.time()) + max(10, settings.connector_dispatch_lease_seconds)


def persist_dispatch(
    record: DispatchRecord,
    status: str = "pending",
) -> None:
    now = time.time()
    try:
        with Session(engine) as session:
            session.add(
                AgentDispatchTask(
                    task_id=record.task_id,
                    user_id=record.user_id,
                    ai_config_id=record.ai_config_id,
                    ai_kind=record.ai_kind or "assistant",
                    session_id=record.session_id,
                    session_name=record.session_name,
                    device_id=record.device_id,
                    tool=record.tool or "",
                    instruction=record.instruction or "",
                    args_json=json.dumps(record.args, ensure_ascii=False, default=str),
                    suppress_session_message=record.suppress_session_message,
                    status=status,
                    updated_at=now,
                    deadline_at=now + _LEGACY_DISPATCH_TTL_SECONDS,
                    owner_instance_id=CONNECTOR_INSTANCE_ID,
                    lease_expires_at=lease_deadline(now),
                    attempt=1,
                )
            )
            session.commit()
    except Exception as exc:
        logger.exception("persist failed task=%s: %s", record.task_id, exc)


def enqueue_dispatch_row(
    record: DispatchRecord,
    timeout_seconds: int = 120,
) -> str:
    with _DISPATCH_QUEUE_LOCK:
        with Session(engine) as session:
            ahead = session.exec(
                select(AgentDispatchTask).where(
                    AgentDispatchTask.device_id == record.device_id,
                    AgentDispatchTask.status.in_(["pending", "queued"]),
                )
            ).first()
            status = "queued" if ahead else "pending"
            now = time.time()
            session.add(
                AgentDispatchTask(
                    task_id=record.task_id,
                    user_id=record.user_id,
                    ai_config_id=record.ai_config_id,
                    ai_kind=record.ai_kind or "assistant",
                    session_id=record.session_id,
                    session_name=record.session_name,
                    device_id=record.device_id,
                    tool=record.tool or "",
                    instruction=record.instruction or "",
                    args_json=json.dumps(record.args, ensure_ascii=False, default=str),
                    suppress_session_message=record.suppress_session_message,
                    status=status,
                    updated_at=now,
                    deadline_at=now + max(1, int(timeout_seconds or 120)),
                    owner_instance_id=CONNECTOR_INSTANCE_ID,
                    lease_expires_at=lease_deadline(now),
                    attempt=1,
                )
            )
            session.commit()
            return status


def claim_next_queued(device_id: str) -> Optional[AgentDispatchTask]:
    """Atomically promote the oldest queued task for an idle device."""
    with _DISPATCH_QUEUE_LOCK:
        with Session(engine) as session:
            active = session.exec(
                select(AgentDispatchTask).where(
                    AgentDispatchTask.device_id == device_id,
                    AgentDispatchTask.status == "pending",
                )
            ).first()
            if active:
                return None
            row = session.exec(
                select(AgentDispatchTask).where(
                    AgentDispatchTask.device_id == device_id,
                    AgentDispatchTask.status == "queued",
                ).order_by(AgentDispatchTask.created_at, AgentDispatchTask.id)
            ).first()
            if not row:
                return None
            require_transition(row.status, "pending")
            now = time.time()
            row.status = "pending"
            row.owner_instance_id = CONNECTOR_INSTANCE_ID
            row.lease_expires_at = lease_deadline(now)
            row.updated_at = now
            row.attempt = int(row.attempt or 0) + 1
            session.add(row)
            session.commit()
            session.refresh(row)
            session.expunge(row)
            return row


def requeue_pending(task_id: str) -> bool:
    """Return an un-emitted pending task to the queue after transport failure."""
    with Session(engine) as session:
        row = session.exec(
            select(AgentDispatchTask).where(AgentDispatchTask.task_id == task_id)
        ).first()
        if not row or row.status != "pending":
            return False
        row.status = "queued"
        row.updated_at = time.time()
        row.lease_expires_at = lease_deadline()
        session.add(row)
        session.commit()
        return True


def finalize_dispatch_row(
    task_id: str,
    *,
    status: str,
    success: Optional[bool] = None,
    summary: Optional[str] = None,
    result: Any = None,
    error: Optional[str] = None,
) -> bool:
    try:
        with Session(engine) as session:
            row = session.exec(
                select(AgentDispatchTask).where(AgentDispatchTask.task_id == task_id)
            ).first()
            terminal = {state.value for state in TERMINAL_DISPATCH_STATUSES}
            if not row or row.status in terminal:
                return False
            require_transition(row.status, status)
            now = time.time()
            row.status = status
            row.success = success
            row.summary = summary
            if result is not None:
                row.result_json = json.dumps(result, ensure_ascii=False, default=str)
            row.error = error
            row.completed_at = now
            row.updated_at = now
            row.lease_expires_at = None
            session.add(row)
            session.commit()
            return True
    except Exception as exc:
        logger.exception("finalize failed task=%s: %s", task_id, exc)
        return False


def expire_orphan_dispatches(older_than_seconds: float = 300.0) -> int:
    now = time.time()
    cutoff = now - older_than_seconds
    expired = 0
    try:
        with Session(engine) as session:
            rows = session.exec(
                select(AgentDispatchTask).where(
                    AgentDispatchTask.status.in_(["pending", "queued"])
                )
            ).all()
            for row in rows:
                previous_owner = bool(
                    row.owner_instance_id and row.owner_instance_id != CONNECTOR_INSTANCE_ID
                )
                lease_expired = bool(row.lease_expires_at and row.lease_expires_at <= now)
                deadline_expired = bool(row.deadline_at and row.deadline_at <= now)
                legacy_expired = not row.owner_instance_id and (row.created_at or 0) < cutoff
                if not (previous_owner or lease_expired or deadline_expired or legacy_expired):
                    continue
                stale_status = row.status
                require_transition(stale_status, "timeout")
                row.status = "timeout"
                row.error = row.error or _orphan_reason(previous_owner, lease_expired, stale_status)
                row.completed_at = now
                row.updated_at = now
                row.lease_expires_at = None
                session.add(row)
                expired += 1
            if expired:
                session.commit()
    except Exception as exc:
        logger.exception("orphan sweep failed: %s", exc)
    return expired


def _orphan_reason(previous_owner: bool, lease_expired: bool, status: str) -> str:
    if previous_owner:
        return "orphaned from a previous connector-runtime instance"
    if lease_expired:
        return "connector dispatch lease expired"
    if status == "queued":
        return "expired in device queue before dispatch"
    return "connector dispatch deadline expired"
