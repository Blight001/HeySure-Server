"""Transactional cancellation for workflow runs and device dispatch rows."""

import time
from typing import Callable

from sqlmodel import Session, select

from api.models import (
    AgentDispatchTask,
    WorkflowConfirmation,
    WorkflowRun,
    WorkflowStepRun,
)

def cancel_workflow_run(
    session: Session,
    run: WorkflowRun,
    reason: str,
    fail_run: Callable,
) -> WorkflowRun:
    if run.status in {"succeeded", "failed", "cancelled", "timed_out"}:
        return run
    steps = session.exec(
        select(WorkflowStepRun).where(
            WorkflowStepRun.run_id == run.id,
            WorkflowStepRun.status.in_(["dispatch_pending", "dispatching", "waiting_device"]),
        )
    ).all()
    fail_run(
        session,
        run,
        {"code": "RUN_CANCELLED", "message": reason, "phase": "cancel", "retryable": False},
        status="cancelled",
    )
    _cancel_dispatch_rows(session, steps, reason)
    now = time.time()
    for step in steps:
        step.status = "cancelled"
        step.finished_at = now
        session.add(step)
    confirmations = session.exec(
        select(WorkflowConfirmation).where(
            WorkflowConfirmation.run_id == run.id,
            WorkflowConfirmation.status == "pending",
        )
    ).all()
    for confirmation in confirmations:
        confirmation.status = "cancelled"
        confirmation.decision = "cancelled"
        confirmation.decided_at = now
        session.add(confirmation)
    session.commit()
    session.refresh(run)
    return run


def _cancel_dispatch_rows(session: Session, steps, reason: str) -> None:
    task_ids = [str(step.dispatch_task_id or "") for step in steps if step.dispatch_task_id]
    if not task_ids:
        return
    rows = session.exec(
        select(AgentDispatchTask).where(
            AgentDispatchTask.task_id.in_(task_ids),
            AgentDispatchTask.status.in_(["queued", "pending"]),
        )
    ).all()
    now = time.time()
    for row in rows:
        row.status = "cancelled"
        row.success = False
        row.error = reason
        row.completed_at = now
        row.updated_at = now
        row.lease_expires_at = None
        session.add(row)
