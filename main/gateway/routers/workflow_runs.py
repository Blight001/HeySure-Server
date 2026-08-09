"""Authenticated workflow run creation, status, history and cancellation."""

from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlmodel import Session, select

from api.database import get_session
from api.models import (
    WorkflowAuditEvent,
    WorkflowConfirmation,
    WorkflowRun,
    WorkflowSchedulerHeartbeat,
    WorkflowStepRun,
)
from api.services.workflows.result_store import load_result
from api.services.workflows.run_service import (
    cancel_run,
    confirmation_payload,
    create_run,
    decide_confirmation,
    retry_failed_run,
    run_payload,
    step_payload,
)
from api.services.workflows.schemas import RunCancel, RunConfirm, RunCreate, RunRetry
from api.core.settings import settings
from api.runtime.internal_http import internal_post
from .auth import get_current_user


logger = logging.getLogger(__name__)


def _require_enabled() -> None:
    if not settings.workflow_cards_enabled:
        raise HTTPException(status_code=404, detail="workflow cards are disabled")


router = APIRouter(dependencies=[Depends(_require_enabled)])
PREFIX = "/api"


def _owned_run(session: Session, user_id: int, run_id: str):
    return session.exec(
        select(WorkflowRun).where(WorkflowRun.id == run_id, WorkflowRun.user_id == user_id)
    ).first()


async def _cancel_device_dispatch(task_id: str, reason: str) -> None:
    if settings.connector_runtime_url:
        await internal_post(
            settings.connector_runtime_url,
            f"/internal/agent/dispatch/cancel/{task_id}",
            json={"reason": reason},
            timeout=10.0,
        )
        return
    from connector_runtime.dispatch.device_dispatch import cancel_dispatch

    await cancel_dispatch(task_id, reason)


@router.get("/workflow-metrics")
def workflow_metrics(
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    rows = session.exec(select(WorkflowRun).where(WorkflowRun.user_id == user.id)).all()
    steps = session.exec(
        select(WorkflowStepRun).where(
            WorkflowStepRun.run_id.in_([row.id for row in rows] or ["__none__"])
        )
    ).all()
    audits = session.exec(select(WorkflowAuditEvent).where(WorkflowAuditEvent.user_id == user.id)).all()
    heartbeats = session.exec(
        select(WorkflowSchedulerHeartbeat).order_by(WorkflowSchedulerHeartbeat.heartbeat_at.desc())
    ).all()
    status_counts: dict[str, int] = {}
    durations = []
    for row in rows:
        status_counts[row.status] = status_counts.get(row.status, 0) + 1
        if row.started_at is not None and row.finished_at is not None:
            durations.append(max(0.0, row.finished_at - row.started_at))
    succeeded = status_counts.get("succeeded", 0)
    terminal = sum(status_counts.get(item, 0) for item in ("succeeded", "failed", "cancelled", "timed_out"))
    now = time.time()
    newest_heartbeat = heartbeats[0] if heartbeats else None
    heartbeat_age = max(0.0, now - newest_heartbeat.heartbeat_at) if newest_heartbeat else None
    event_counts: dict[str, int] = {}
    for event in audits:
        event_counts[event.event_type] = event_counts.get(event.event_type, 0) + 1
    stale_threshold = max(30.0, float(settings.workflow_scheduler_interval_seconds) * 10)
    return {
        "runs_total": len(rows),
        "status_counts": status_counts,
        "success_rate": (succeeded / terminal) if terminal else None,
        "average_duration_seconds": (sum(durations) / len(durations)) if durations else None,
        "outbox_backlog": sum(step.status in {"dispatch_pending", "dispatching"} for step in steps),
        "waiting_device_steps": sum(step.status == "waiting_device" for step in steps),
        "step_failures": sum(step.status in {"failed", "timed_out"} for step in steps),
        "retry_attempts": sum(max(0, step.attempt - 1) for step in steps),
        "offline_recoveries": event_counts.get("device_reconnected", 0),
        "confirmation_denials": sum(
            event.event_type == "confirmation_decided" and '\"decision\":\"denied\"' in event.detail_json
            for event in audits
        ),
        "schema_incompatible": sum("TOOL_SCHEMA_INCOMPATIBLE" in event.detail_json for event in audits),
        "permission_denials": sum("TOOL_PERMISSION_DENIED" in event.detail_json for event in audits),
        "ignored_terminal_results": event_counts.get("step_result_ignored", 0),
        "scheduler_instances": len([item for item in heartbeats if now - item.heartbeat_at <= stale_threshold]),
        "scheduler_heartbeat_age_seconds": heartbeat_age,
        "scheduler_healthy": heartbeat_age is not None and heartbeat_age <= stale_threshold,
        "scheduler_last_error": newest_heartbeat.last_error if newest_heartbeat else "not started",
        "stalled_runs": sum(
            row.status not in {"succeeded", "failed", "cancelled", "timed_out"}
            and now - row.updated_at > stale_threshold
            for row in rows
        ),
    }


@router.post("/workflow-cards/{card_id}/runs", status_code=202)
def start_run(
    card_id: str,
    body: RunCreate,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    if not settings.workflow_scheduler_enabled:
        raise HTTPException(status_code=503, detail={"code": "WORKFLOW_SCHEDULER_DISABLED"})
    user = get_current_user(authorization, session)
    try:
        row = create_run(
            session,
            user_id=user.id,
            card_id=card_id,
            device_id=body.device_id,
            input_value=body.input,
            version_id=body.version_id,
            idempotency_key=body.idempotency_key,
        )
    except ValueError as exc:
        detail = str(exc)
        code = detail.split(":", 1)[0]
        status_code = 404 if code in {"CARD_NOT_FOUND"} else 409 if code == "CARD_VERSION_NOT_RUNNABLE" else 422
        raise HTTPException(status_code=status_code, detail={"code": code, "message": detail})
    return run_payload(row)


@router.get("/workflow-runs")
def list_runs(
    card_id: Optional[str] = None,
    device_id: Optional[str] = None,
    status: Optional[str] = None,
    created_from: Optional[float] = None,
    created_to: Optional[float] = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    statement = select(WorkflowRun).where(WorkflowRun.user_id == user.id)
    if card_id:
        statement = statement.where(WorkflowRun.card_id == card_id)
    if device_id:
        statement = statement.where(WorkflowRun.device_id == device_id)
    if status:
        statement = statement.where(WorkflowRun.status == status)
    if created_from is not None:
        statement = statement.where(WorkflowRun.created_at >= created_from)
    if created_to is not None:
        statement = statement.where(WorkflowRun.created_at <= created_to)
    rows = session.exec(statement.order_by(WorkflowRun.created_at.desc()).offset(offset).limit(limit)).all()
    return {"items": [run_payload(row) for row in rows], "limit": limit, "offset": offset}


@router.get("/workflow-runs/{run_id}")
def get_run(
    run_id: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    row = _owned_run(session, user.id, run_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND"})
    return run_payload(row)


@router.get("/workflow-runs/{run_id}/steps")
def get_steps(
    run_id: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    row = _owned_run(session, user.id, run_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND"})
    steps = session.exec(
        select(WorkflowStepRun)
        .where(WorkflowStepRun.run_id == row.id)
        .order_by(WorkflowStepRun.started_at, WorkflowStepRun.id)
    ).all()
    return {"items": [step_payload(step) for step in steps]}


@router.post("/workflow-runs/{run_id}/cancel")
async def cancel(
    run_id: str,
    body: RunCancel,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    row = _owned_run(session, user.id, run_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND"})
    dispatch_ids = session.exec(
        select(WorkflowStepRun.dispatch_task_id).where(
            WorkflowStepRun.run_id == row.id,
            WorkflowStepRun.status.in_(["dispatch_pending", "dispatching", "waiting_device"]),
        )
    ).all()
    cancelled = cancel_run(session, row, body.reason)
    for task_id in dispatch_ids:
        if task_id:
            try:
                await _cancel_device_dispatch(str(task_id), body.reason)
            except Exception:
                logger.exception("device dispatch cancel notification failed task=%s", task_id)
    return run_payload(cancelled)


@router.get("/workflow-runs/{run_id}/confirmations")
def confirmations(
    run_id: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    row = _owned_run(session, user.id, run_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND"})
    items = session.exec(
        select(WorkflowConfirmation)
        .where(WorkflowConfirmation.run_id == run_id)
        .order_by(WorkflowConfirmation.created_at.desc())
    ).all()
    return {"items": [confirmation_payload(item) for item in items]}


@router.post("/workflow-runs/{run_id}/confirm")
def confirm(
    run_id: str,
    body: RunConfirm,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    row = _owned_run(session, user.id, run_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND"})
    try:
        return run_payload(decide_confirmation(session, run=row, user_id=user.id, approved=body.approved))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": str(exc)})


@router.post("/workflow-runs/{run_id}/retry", status_code=202)
def retry(
    run_id: str,
    body: RunRetry,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    row = _owned_run(session, user.id, run_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND"})
    try:
        return run_payload(
            retry_failed_run(
                session,
                run=row,
                user_id=user.id,
                idempotency_key=body.idempotency_key,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": str(exc)})


@router.get("/workflow-runs/{run_id}/steps/{step_run_id}/result")
def get_step_result(
    run_id: str,
    step_run_id: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    run = _owned_run(session, user.id, run_id)
    if not run:
        raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND"})
    step = session.exec(
        select(WorkflowStepRun).where(WorkflowStepRun.id == step_run_id, WorkflowStepRun.run_id == run.id)
    ).first()
    if not step:
        raise HTTPException(status_code=404, detail={"code": "STEP_NOT_FOUND"})
    if not step.result_ref:
        return {"result": None, "reference": None}
    try:
        value = load_result(step.result_ref, run.user_id, run.id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail={"code": "RESULT_NOT_FOUND"})
    return {"result": value, "reference": step.result_ref}


@router.get("/workflow-runs/{run_id}/audit")
def get_audit(
    run_id: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    import json

    user = get_current_user(authorization, session)
    run = _owned_run(session, user.id, run_id)
    if not run:
        raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND"})
    rows = session.exec(
        select(WorkflowAuditEvent)
        .where(WorkflowAuditEvent.run_id == run.id)
        .order_by(WorkflowAuditEvent.created_at, WorkflowAuditEvent.id)
    ).all()
    return {
        "items": [
            {
                "id": item.id,
                "event_type": item.event_type,
                "step_id": item.step_id,
                "dispatch_task_id": item.dispatch_task_id,
                "status_from": item.status_from,
                "status_to": item.status_to,
                "detail": json.loads(item.detail_json or "{}"),
                "created_at": item.created_at,
            }
            for item in rows
        ]
    }
