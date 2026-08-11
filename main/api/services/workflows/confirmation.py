"""Human confirmation state transitions for workflow runs."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

from sqlalchemy import or_
from sqlmodel import Session, select

from api.models import WorkflowCardVersion, WorkflowConfirmation, WorkflowRun, WorkflowStepRun

from .audit import add_audit


RUN_CONFIRMATION_SCOPE = "__run__"
DISPATCH_CONFIRMATION_TYPES = {"forced", "user_via_ai_dispatch"}


def _load(raw: Optional[str], fallback: Any) -> Any:
    try:
        return json.loads(raw or "")
    except Exception:
        return fallback


def _assigned_ai_id(run: WorkflowRun, confirmation_type: str) -> Optional[int]:
    if confirmation_type != "user_via_ai_dispatch" or run.actor_type != "ai":
        return None
    try:
        value = int(run.actor_id)
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


def request_confirmation(
    session: Session,
    *,
    run: WorkflowRun,
    step_id: str,
    confirmation_type: str,
    risk_summary: str,
    timeout_seconds: int,
    next_step_id: str = "",
    on_denied_step_id: str = "",
) -> WorkflowConfirmation:
    existing = session.exec(
        select(WorkflowConfirmation).where(
            WorkflowConfirmation.run_id == run.id,
            WorkflowConfirmation.step_id == step_id,
            WorkflowConfirmation.status.in_(["pending", "approved"]),
        ).order_by(WorkflowConfirmation.created_at.desc())
    ).first()
    if existing:
        return existing
    now = time.time()
    ai_config_id = _assigned_ai_id(run, confirmation_type)
    row = WorkflowConfirmation(
        id=f"wconf_{uuid.uuid4().hex}", run_id=run.id, step_id=step_id,
        confirmation_type=confirmation_type, risk_summary=risk_summary[:2000],
        next_step_id=next_step_id, on_denied_step_id=on_denied_step_id,
        requested_user_id=run.user_id, ai_config_id=ai_config_id,
        expires_at=min(run.deadline_at, now + max(1, int(timeout_seconds))),
    )
    previous = run.status
    run.status = "waiting_ai" if ai_config_id else "waiting_confirmation"
    run.next_wakeup_at = row.expires_at
    run.updated_at = now
    run.lock_version += 1
    session.add(row)
    session.add(run)
    add_audit(
        session, event_type="ai_interaction_requested" if ai_config_id else "confirmation_requested",
        run=run, step_id=step_id, status_from=previous, status_to=run.status,
        detail={"confirmation_id": row.id, "type": confirmation_type, "risk_summary": risk_summary[:500]},
    )
    return row


def confirmation_granted(session: Session, run_id: str, step_id: str) -> bool:
    row = session.exec(select(WorkflowConfirmation).where(
        WorkflowConfirmation.run_id == run_id,
        or_(
            WorkflowConfirmation.step_id == step_id,
            WorkflowConfirmation.next_step_id == RUN_CONFIRMATION_SCOPE,
        ),
        WorkflowConfirmation.confirmation_type.in_(DISPATCH_CONFIRMATION_TYPES),
        WorkflowConfirmation.status == "approved",
    ).order_by(WorkflowConfirmation.decided_at.desc())).first()
    return bool(row)


def renew_dispatch_step_deadline(
    session: Session, run: WorkflowRun, step_id: str, now: Optional[float] = None,
) -> None:
    current = float(now or time.time())
    version = session.get(WorkflowCardVersion, run.card_version_id)
    definition = _load(version.definition_json, {}) if version else {}
    step_definition = definition.get("steps", {}).get(step_id, {})
    timeout = max(1, int(step_definition.get("timeoutSeconds", 120)))
    step_run = session.exec(select(WorkflowStepRun).where(
        WorkflowStepRun.run_id == run.id,
        WorkflowStepRun.step_id == step_id,
        WorkflowStepRun.status.in_(["dispatch_pending", "dispatching"]),
    ).order_by(WorkflowStepRun.attempt.desc())).first()
    if not step_run:
        return
    step_run.status = "dispatch_pending"
    step_run.deadline_at = min(run.deadline_at, current + timeout)
    step_run.claim_owner = ""
    step_run.claimed_at = None
    session.add(step_run)


def _approve(run: WorkflowRun, item: WorkflowConfirmation, session: Session, now: float) -> None:
    if item.confirmation_type in {"explicit", "user_via_ai"}:
        run.current_step_id = item.next_step_id
        run.status = "running"
    elif item.confirmation_type == "ai_review":
        raise ValueError("AI_REVIEW_REQUIRES_AI")
    else:
        run.status = "waiting_device"
        renew_dispatch_step_deadline(session, run, item.step_id, now)
    run.next_wakeup_at = now
    run.updated_at = now
    run.lock_version += 1
    session.add(run)


def _deny(session: Session, run: WorkflowRun, item: WorkflowConfirmation, now: float) -> None:
    if item.on_denied_step_id:
        run.current_step_id = item.on_denied_step_id
        run.status = "running"
        run.next_wakeup_at = now
        run.updated_at = now
        run.lock_version += 1
        session.add(run)
        return
    from .run_service import error_payload, fail_run
    fail_run(
        session, run,
        error_payload("CONFIRMATION_DENIED", "workflow confirmation was denied or expired", "confirmation"),
        status="cancelled",
    )


def decide_confirmation(
    session: Session, *, run: WorkflowRun, user_id: int, approved: bool,
) -> WorkflowRun:
    item = session.exec(select(WorkflowConfirmation).where(
        WorkflowConfirmation.run_id == run.id,
        WorkflowConfirmation.status == "pending",
    ).order_by(WorkflowConfirmation.created_at.desc()).with_for_update(skip_locked=True)).first()
    if not item:
        raise ValueError("CONFIRMATION_NOT_PENDING")
    if item.requested_user_id != user_id:
        raise ValueError("CONFIRMATION_ACCESS_DENIED")
    now = time.time()
    approved = bool(approved and now < item.expires_at)
    item.status = "approved" if approved else "denied"
    item.decision = item.status
    item.decided_by = user_id
    item.decided_at = now
    session.add(item)
    _approve(run, item, session, now) if approved else _deny(session, run, item, now)
    add_audit(
        session, event_type="confirmation_decided", run=run, step_id=item.step_id,
        status_from="waiting_ai" if item.ai_config_id else "waiting_confirmation", status_to=run.status,
        detail={"confirmation_id": item.id, "decision": item.status},
    )
    session.commit()
    session.refresh(run)
    return run


def expire_confirmations(session: Session, now: Optional[float] = None, limit: int = 100) -> int:
    current = float(now or time.time())
    rows = session.exec(select(WorkflowConfirmation).where(
        WorkflowConfirmation.status == "pending",
        WorkflowConfirmation.expires_at <= current,
    ).limit(limit)).all()
    for item in rows:
        run = session.get(WorkflowRun, item.run_id)
        item.status = "expired"
        item.decision = "expired"
        item.decided_at = current
        session.add(item)
        if run and run.status == "waiting_confirmation":
            _deny(session, run, item, current)
            add_audit(
                session, event_type="confirmation_expired", run=run, step_id=item.step_id,
                status_from="waiting_confirmation", status_to=run.status,
            )
    if rows:
        session.commit()
    return len(rows)
