"""Authenticated workflow run creation, status, history and cancellation."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlmodel import Session, select

from api.database import get_session
from api.models import WorkflowRun, WorkflowStepRun
from api.services.workflows.run_service import cancel_run, create_run, run_payload, step_payload
from api.services.workflows.schemas import RunCancel, RunCreate
from api.core.settings import settings
from .auth import get_current_user


def _require_enabled() -> None:
    if not settings.workflow_cards_enabled:
        raise HTTPException(status_code=404, detail="workflow cards are disabled")


router = APIRouter(dependencies=[Depends(_require_enabled)])
PREFIX = "/api"


def _owned_run(session: Session, user_id: int, run_id: str):
    return session.exec(
        select(WorkflowRun).where(WorkflowRun.id == run_id, WorkflowRun.user_id == user_id)
    ).first()


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
def cancel(
    run_id: str,
    body: RunCancel,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    row = _owned_run(session, user.id, run_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND"})
    return run_payload(cancel_run(session, row, body.reason))
