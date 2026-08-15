"""User-facing maintenance work-order API."""

from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from api.core.settings import settings
from api.database import get_session
from api.models.maintenance import MaintenanceApproval, MaintenanceEvent, MaintenanceTask
from api.runtime.internal_http import internal_post
from api.services.maintenance import (
    CreateTaskSpec, EventRecord, MaintenanceConflict, MaintenanceNotFound, MaintenanceService,
)
from api.services.maintenance.views import approval_dto as _approval_dto
from api.services.maintenance.views import event_dto as _event_dto
from api.services.maintenance.views import task_dto as _task_dto
from api.services.maintenance.views import run_start_payload as _run_start_payload
from api.sio import sio

from .auth import get_current_user


router = APIRouter()
PREFIX = "/api/maintenance"


class CreateTaskRequest(BaseModel):
    maintainer_ai_config_id: int
    device_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1, max_length=200_000)
    acceptance_criteria: str = Field(default="", max_length=100_000)
    affected_repo: str = Field(default="", max_length=200)
    reporter_ai_config_id: Optional[int] = None
    source_session_id: str = Field(default="", max_length=200)
    severity: str = Field(default="normal", max_length=40)
    dedupe_key: str = Field(default="", max_length=200)
    deadline_at: Optional[float] = None


class CommandRequest(BaseModel):
    content: str = Field(default="", max_length=100_000)
    request_id: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=1, max_length=200)


class ApprovalDecisionRequest(BaseModel):
    decision: str
    comment: str = Field(default="", max_length=20_000)
    request_id: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=1, max_length=200)


def _service_error(exc: Exception) -> HTTPException:
    if isinstance(exc, MaintenanceNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=409, detail=str(exc))


async def _emit_update(user_id: int, task: MaintenanceTask, *, event=None, approval=None) -> None:
    payload: dict[str, Any] = {"task_id": task.task_id, "task": _task_dto(task)}
    if event is not None:
        payload["event"] = _event_dto(event)
    if approval is not None:
        payload["approval"] = _approval_dto(approval)
    await sio.emit("maintenance:update", payload, room=f"user_{user_id}")


async def _send_device_command(task: MaintenanceTask, event: str, payload: dict) -> bool:
    connector = str(settings.connector_runtime_url or "").strip()
    if not connector:
        return False
    try:
        await internal_post(
            connector, "/internal/maintenance/command",
            json={"user_id": task.user_id, "task_id": task.task_id, "run_id": task.run_id,
                  "device_id": task.device_id, "ai_config_id": task.maintainer_ai_config_id,
                  "event": event, "payload": payload}, timeout=15,
        )
        return True
    except Exception:
        return False


def _record_waiting(session: Session, task: MaintenanceTask, command_id: str,
                    command: str, payload: dict) -> MaintenanceEvent:
    service = MaintenanceService(session)
    event = service.append_event(task, EventRecord(
        "dispatch.waiting_device", "system",
        {"command_id": command_id, "command": command, "payload": payload},
        event_id=f"waiting:{command_id}",
    ))
    session.commit()
    session.refresh(task)
    session.refresh(event)
    return event


def _stored_command_payload(event: MaintenanceEvent) -> dict:
    payload = MaintenanceService.event_payload(event).get("payload") or {}
    return {key: value for key, value in payload.items() if key not in {"command", "approval_decision"}}


@router.post("/tasks")
async def create_task(body: CreateTaskRequest, session: Session = Depends(get_session),
                      authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization, session)
    service = MaintenanceService(session)
    try:
        row = service.create_task(user.id, CreateTaskSpec(**body.model_dump()))
    except (MaintenanceConflict, MaintenanceNotFound) as exc:
        raise _service_error(exc) from exc
    await _emit_update(user.id, row)
    delivered = await _send_device_command(row, "codex:run_start", _run_start_payload(row))
    if not delivered:
        event = _record_waiting(session, row, f"run_start:{row.run_id}", "run_start", {})
        await _emit_update(user.id, row, event=event)
    return {**_task_dto(row), "delivery_status": "delivered" if delivered else "waiting_device"}


@router.get("/tasks")
def list_tasks(status: str = "", limit: int = Query(default=50, ge=1, le=200),
               session: Session = Depends(get_session), authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization, session)
    statement = select(MaintenanceTask).where(MaintenanceTask.user_id == user.id)
    if status:
        statement = statement.where(MaintenanceTask.status == status)
    rows = session.exec(statement.order_by(MaintenanceTask.updated_at.desc()).limit(limit)).all()
    return {"items": [_task_dto(row) for row in rows]}


@router.get("/tasks/{task_id}")
def get_task(task_id: str, session: Session = Depends(get_session),
             authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization, session)
    service = MaintenanceService(session)
    try:
        row = service.owned_task(user.id, task_id)
    except MaintenanceNotFound as exc:
        raise _service_error(exc) from exc
    approvals = session.exec(select(MaintenanceApproval).where(
        MaintenanceApproval.task_id == task_id, MaintenanceApproval.user_id == user.id,
    ).order_by(MaintenanceApproval.created_at.desc())).all()
    return {"task": _task_dto(row), "approvals": [_approval_dto(item) for item in approvals]}


@router.get("/tasks/{task_id}/events")
def task_events(task_id: str, after_id: int = Query(default=0, ge=0),
                limit: int = Query(default=200, ge=1, le=500),
                session: Session = Depends(get_session), authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization, session)
    service = MaintenanceService(session)
    try:
        service.owned_task(user.id, task_id)
    except MaintenanceNotFound as exc:
        raise _service_error(exc) from exc
    rows = session.exec(select(MaintenanceEvent).where(
        MaintenanceEvent.task_id == task_id, MaintenanceEvent.id > after_id,
    ).order_by(MaintenanceEvent.id.asc()).limit(limit)).all()
    return {"items": [_event_dto(row) for row in rows]}


async def _queue_command(user_id: int, task_id: str, body: CommandRequest,
                         event_name: str, session: Session) -> dict:
    service = MaintenanceService(session)
    try:
        row = service.owned_task(user_id, task_id, lock=True)
        event_id = f"cmd:{body.request_id}"
        existing = session.exec(select(MaintenanceEvent).where(
            MaintenanceEvent.run_id == row.run_id, MaintenanceEvent.event_id == event_id,
        )).first()
        if not existing and row.status in {"succeeded", "failed", "cancelled"}:
            raise MaintenanceConflict("terminal maintenance tasks cannot accept commands")
        event = service.append_event(row, EventRecord(
            f"command.{event_name.rsplit(':', 1)[-1]}", "user",
            {"command_id": body.request_id,
             "command": event_name.rsplit(":", 1)[-1],
             "text" if event_name == "codex:steer" else "reason": body.content},
            event_id=event_id, actor_id=str(user_id),
        ))
        if event_name == "codex:interrupt" and row.status not in {"succeeded", "failed", "cancelled"}:
            service.apply_state(row, status="cancelled")
        session.commit()
        session.refresh(row)
        session.refresh(event)
    except (MaintenanceConflict, MaintenanceNotFound) as exc:
        raise _service_error(exc) from exc
    command_payload = _stored_command_payload(event)
    delivered = await _send_device_command(row, event_name, command_payload)
    if not delivered:
        waiting = _record_waiting(
            session, row, body.request_id, event_name.rsplit(":", 1)[-1], command_payload,
        )
        await _emit_update(user_id, row, event=waiting)
    await _emit_update(user_id, row, event=event)
    return {
        "ok": True, "accepted": True, "task": _task_dto(row),
        "command_id": body.request_id,
        "delivery_status": "delivered" if delivered else "waiting_device",
    }


@router.post("/tasks/{task_id}/steer")
async def steer(task_id: str, body: CommandRequest, session: Session = Depends(get_session),
                authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization, session)
    if not body.content.strip():
        raise HTTPException(status_code=422, detail="content is required")
    return await _queue_command(user.id, task_id, body, "codex:steer", session)


@router.post("/tasks/{task_id}/interrupt")
async def interrupt(task_id: str, body: CommandRequest, session: Session = Depends(get_session),
                    authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization, session)
    return await _queue_command(user.id, task_id, body, "codex:interrupt", session)


@router.post("/approvals/{approval_id}/decision")
async def decide(approval_id: str, body: ApprovalDecisionRequest,
                 session: Session = Depends(get_session), authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization, session)
    service = MaintenanceService(session)
    command_id = f"approval:{approval_id}"
    try:
        task, approval, event = service.decide_approval(
            user.id, approval_id, body.decision, body.comment, command_id,
        )
    except (MaintenanceConflict, MaintenanceNotFound) as exc:
        raise _service_error(exc) from exc
    command_payload = _stored_command_payload(event)
    delivered = await _send_device_command(task, "codex:approval_decision", command_payload)
    if not delivered:
        waiting = _record_waiting(
            session, task, command_id, "approval_decision", command_payload,
        )
        await _emit_update(user.id, task, event=waiting, approval=approval)
    await _emit_update(user.id, task, event=event, approval=approval)
    return {
        "ok": True, "accepted": True, "task": _task_dto(task),
        "approval": _approval_dto(approval),
        "delivery_status": "delivered" if delivered else "waiting_device",
    }
