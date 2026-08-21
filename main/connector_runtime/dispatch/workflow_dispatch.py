"""Bridge persistent workflow step outbox rows to the existing device queue."""

from __future__ import annotations

import json
import logging
import time
import uuid

from sqlmodel import Session, select

from api.database import engine
from api.models import AgentDispatchTask, WorkflowRun, WorkflowStepRun
from api.services.workflows.permissions import WorkflowDispatchError, validate_step_dispatch
from api.services.workflows.run_service import (
    apply_step_result,
    fail_step_dispatch,
    render_step_arguments,
)
from api.services.workflows.step_runtime import step_run_device_id
from connector_runtime.dispatch.device_dispatch import dispatch_task_to_agent, redeliver_dispatch


logger = logging.getLogger(__name__)
CLAIM_OWNER = f"connector-{uuid.uuid4().hex}"
CLAIM_STALE_SECONDS = 30.0


def _decode_result(raw: str):
    try:
        return json.loads(raw or "")
    except Exception:
        return raw or None


def reconcile_finished_dispatches(limit: int = 100) -> int:
    """Advance steps from terminal AgentDispatchTask rows (restart recovery)."""
    applied = 0
    with Session(engine) as session:
        steps = session.exec(
            select(WorkflowStepRun)
            .where(WorkflowStepRun.status.in_(["dispatch_pending", "dispatching", "waiting_device"]))
            .limit(limit)
        ).all()
    for step in steps:
        with Session(engine) as session:
            run = session.get(WorkflowRun, step.run_id)
            if run and run.status in {"waiting_ai", "paused"}:
                continue
            dispatch = session.exec(
                select(AgentDispatchTask).where(AgentDispatchTask.task_id == step.dispatch_task_id)
            ).first()
            if time.time() >= step.deadline_at and (not dispatch or dispatch.status not in {"completed", "error"}):
                applied += int(fail_step_dispatch(
                    session,
                    dispatch_task_id=step.dispatch_task_id,
                    code="STEP_TIMEOUT",
                    message="step deadline elapsed before a terminal device result",
                ))
                continue
            if not dispatch or dispatch.status not in {"completed", "error", "timeout"}:
                continue
            applied += int(apply_step_result(
                session,
                dispatch_task_id=step.dispatch_task_id,
                success=dispatch.status == "completed" and bool(dispatch.success),
                result=_decode_result(dispatch.result_json or ""),
                error=dispatch.error or dispatch.summary,
            ))
    return applied


async def dispatch_pending_steps(limit: int = 50) -> int:
    sent = 0
    with Session(engine) as session:
        rows = session.exec(
            select(WorkflowStepRun)
            .where(WorkflowStepRun.status.in_(["dispatch_pending", "dispatching"]))
            .order_by(WorkflowStepRun.deadline_at, WorkflowStepRun.id)
            .limit(limit)
        ).all()
    for snapshot in rows:
        now = time.time()
        action = ""
        prepared = None
        with Session(engine) as session:
            step = session.exec(
                select(WorkflowStepRun)
                .where(WorkflowStepRun.id == snapshot.id)
                .with_for_update(skip_locked=True)
            ).first()
            run = session.get(WorkflowRun, step.run_id) if step else None
            if not step or not run:
                continue
            if step.status == "dispatching" and (step.claimed_at or 0) > now - CLAIM_STALE_SECONDS:
                continue
            if step.status not in {"dispatch_pending", "dispatching"}:
                continue
            if run.status in {"cancelled", "failed", "succeeded", "timed_out"}:
                continue
            if run.status in {"waiting_ai", "paused"}:
                continue
            if run.status == "paused_offline" and (run.next_wakeup_at or 0) > now:
                continue
            step.status = "dispatching"
            step.claim_owner = CLAIM_OWNER
            step.claimed_at = now
            session.add(step)
            session.commit()
            if now >= min(step.deadline_at, run.deadline_at):
                fail_step_dispatch(
                    session,
                    dispatch_task_id=step.dispatch_task_id,
                    code="STEP_TIMEOUT",
                    message="step deadline elapsed before a terminal device result",
                )
                continue
            existing = session.exec(
                select(AgentDispatchTask).where(AgentDispatchTask.task_id == step.dispatch_task_id)
            ).first()
            if existing:
                if existing.status in {"completed", "error", "timeout"}:
                    continue
                action = "redeliver"
                prepared = {"task_id": step.dispatch_task_id}
            else:
                try:
                    arguments = render_step_arguments(session, step)
                    device = validate_step_dispatch(
                        session,
                        user_id=run.user_id,
                        device_id=step_run_device_id(session, step),
                        tool_name=step.tool_name,
                        expected_provider=step.tool_provider,
                        expected_digest=step.tool_schema_digest,
                        arguments=arguments,
                        card_id=run.card_id,
                        card_version_id=run.card_version_id,
                    )
                except WorkflowDispatchError as exc:
                    if exc.code == "DEVICE_OFFLINE":
                        run.status = "paused_offline"
                        step.status = "dispatch_pending"
                        step.claim_owner = ""
                        step.claimed_at = None
                        run.next_wakeup_at = min(run.deadline_at, now + 5.0)
                        run.updated_at = now
                        session.add(run)
                        session.commit()
                        continue
                    fail_step_dispatch(
                        session,
                        dispatch_task_id=step.dispatch_task_id,
                        code=exc.code,
                        message=str(exc),
                        retryable=exc.retryable,
                    )
                    continue
                except Exception as exc:
                    fail_step_dispatch(
                        session,
                        dispatch_task_id=step.dispatch_task_id,
                        code="ARGUMENT_VALIDATION_FAILED",
                        message=str(exc),
                    )
                    continue
                action = "dispatch"
                prepared = {
                    "step_db_id": step.id,
                    "run_id": run.id,
                    "device_id": step_run_device_id(session, step),
                    "user_id": run.user_id,
                    "ai_config_id": device.ai_config_id,
                    "step_id": step.step_id,
                    "task_id": step.dispatch_task_id,
                    "tool": step.tool_name,
                    "arguments": arguments,
                }

        # Network I/O is deliberately outside the database session/transaction.
        if action == "redeliver" and prepared:
            try:
                delivered = await redeliver_dispatch(prepared["task_id"])
            except Exception:
                logger.exception("workflow dispatch redelivery failed task=%s", prepared["task_id"])
                continue
            if delivered:
                with Session(engine) as session:
                    step = session.exec(select(WorkflowStepRun).where(WorkflowStepRun.dispatch_task_id == prepared["task_id"])).first()
                    run = session.get(WorkflowRun, step.run_id) if step else None
                    if step and run and step.status == "dispatching" and step.claim_owner == CLAIM_OWNER:
                        step.status = "waiting_device"
                        step.claim_owner = ""
                        step.claimed_at = None
                        step.started_at = step.started_at or now
                        run.status = "waiting_device"
                        run.next_wakeup_at = step.deadline_at
                        run.updated_at = now
                        session.add(step)
                        session.add(run)
                        session.commit()
                        sent += 1
            continue

        if action == "dispatch" and prepared:
            try:
                outcome = await dispatch_task_to_agent(
                    device_id=prepared["device_id"],
                    user_id=prepared["user_id"],
                    ai_config_id=prepared["ai_config_id"],
                    ai_kind="workflow",
                    session_id="",
                    session_name=None,
                    model=None,
                    instruction=f"Run workflow {prepared['run_id']} step {prepared['step_id']}",
                    tool=prepared["tool"],
                    args=prepared["arguments"],
                    allowed_tools=[prepared["tool"]],
                    wait_for_result=False,
                    suppress_session_message=True,
                    task_id=prepared["task_id"],
                )
            except Exception as exc:
                logger.exception("workflow dispatch failed run=%s step=%s", prepared["run_id"], prepared["step_id"])
                # No dispatch row means the outbox stays pending for compensation.
                continue
            with Session(engine) as session:
                step = session.get(WorkflowStepRun, prepared["step_db_id"])
                run = session.get(WorkflowRun, prepared["run_id"])
                if not step or not run or step.status != "dispatching" or step.claim_owner != CLAIM_OWNER:
                    continue
                if outcome.get("success"):
                    step.status = "waiting_device"
                    step.claim_owner = ""
                    step.claimed_at = None
                    step.started_at = now
                    run.status = "waiting_device"
                    run.next_wakeup_at = step.deadline_at
                    run.updated_at = now
                    session.add(step)
                    session.add(run)
                    session.commit()
                    sent += 1
                else:
                    run.status = "paused_offline"
                    step.status = "dispatch_pending"
                    step.claim_owner = ""
                    step.claimed_at = None
                    run.next_wakeup_at = min(run.deadline_at, time.time() + 5.0)
                    run.updated_at = time.time()
                    session.add(run)
                    session.add(step)
                    session.commit()
    return sent
