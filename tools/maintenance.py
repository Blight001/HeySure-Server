"""Structured maintenance work-order tools used by digital members."""

from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from api.core.settings import settings
from api.database import engine
from api.models import AssistantAIConfig, DeviceAiBinding, DevicePresence
from api.models.maintenance import MaintenanceEvent, MaintenanceTask
from api.runtime.internal_http import internal_post
from api.services.maintenance import (
    CreateTaskSpec, EventRecord, MaintenanceConflict, MaintenanceNotFound, MaintenanceService,
)
from api.services.maintenance.views import event_dto, run_start_payload, task_dto
from api.sio import sio


MAINTENANCE_REQUEST_SCHEMA = {
    "type": "object",
    "required": ["title", "description", "acceptance_criteria", "affected_repo"],
    "properties": {
        "maintainer_ai_config_id": {"type": "integer", "description": "维护负责人（通常为德克萨斯）的成员 ID。"},
        "maintainer_name": {"type": "string", "description": "未提供 ID 时按名字选择维护负责人。"},
        "device_id": {"type": "string", "description": "可选；省略时选择该负责人绑定的 codex-maintainer 设备。"},
        "title": {"type": "string"}, "description": {"type": "string"},
        "acceptance_criteria": {"type": "string"}, "affected_repo": {"type": "string"},
        "source_session_id": {"type": "string"},
        "severity": {"type": "string", "enum": ["low", "normal", "high", "critical"]},
        "dedupe_key": {"type": "string"}, "deadline_at": {"type": "number"},
    },
}

MAINTENANCE_STATUS_SCHEMA = {
    "type": "object",
    "properties": {"task_id": {"type": "string"}, "status": {"type": "string"},
                   "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
}

MAINTENANCE_COMMENT_SCHEMA = {
    "type": "object", "required": ["task_id", "content"],
    "properties": {"task_id": {"type": "string"}, "content": {"type": "string"},
                   "request_id": {"type": "string"}},
}


def _maintainer(session: Session, user_id: int, args: dict) -> AssistantAIConfig:
    raw_id = args.get("maintainer_ai_config_id")
    statement = select(AssistantAIConfig).where(AssistantAIConfig.user_id == user_id)
    if raw_id is not None:
        statement = statement.where(AssistantAIConfig.id == int(raw_id))
    else:
        name = str(args.get("maintainer_name") or "德克萨斯").strip()
        statement = statement.where(AssistantAIConfig.name == name)
    row = session.exec(statement).first()
    if not row:
        raise HTTPException(status_code=404, detail="maintenance owner member not found")
    return row


def _device(session: Session, user_id: int, ai_config_id: int, wanted: str) -> DevicePresence:
    statement = select(DevicePresence).join(
        DeviceAiBinding, DeviceAiBinding.device_id == DevicePresence.device_id,
    ).where(
        DevicePresence.user_id == user_id,
        DevicePresence.platform == "codex-maintainer",
        DeviceAiBinding.user_id == user_id,
        DeviceAiBinding.ai_config_id == ai_config_id,
    )
    if wanted:
        statement = statement.where(DevicePresence.device_id == wanted)
    row = session.exec(statement.order_by(DevicePresence.online.desc(), DevicePresence.updated_at.desc())).first()
    if not row:
        raise HTTPException(status_code=409, detail="maintainer has no bound codex-maintainer device")
    return row


async def _dispatch(task: MaintenanceTask, event: str, payload: dict) -> bool:
    connector = str(settings.connector_runtime_url or "").strip()
    if not connector:
        return False
    try:
        await internal_post(connector, "/internal/maintenance/command", json={
            "user_id": task.user_id, "task_id": task.task_id, "run_id": task.run_id,
            "device_id": task.device_id, "ai_config_id": task.maintainer_ai_config_id,
            "event": event, "payload": payload,
        }, timeout=15)
        return True
    except Exception:
        return False


def _record_waiting(task_id: str, command_id: str, command: str, payload: dict) -> MaintenanceEvent:
    with Session(engine) as session:
        service = MaintenanceService(session)
        existing = session.get(MaintenanceTask, task_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="maintenance task not found")
        task = service.owned_task(existing.user_id, task_id, lock=True)
        event = service.append_event(task, EventRecord(
            "dispatch.waiting_device", "system",
            {"command_id": command_id, "command": command, "payload": payload},
            event_id=f"waiting:{command_id}",
        ))
        session.commit()
        session.refresh(event)
        return event


async def _maintenance_request(user_id: int, args: dict, ai_config_id: Optional[int] = None):
    with Session(engine) as session:
        maintainer = _maintainer(session, user_id, args)
        device = _device(session, user_id, int(maintainer.id), str(args.get("device_id") or ""))
        service = MaintenanceService(session)
        try:
            task = service.create_task(user_id, CreateTaskSpec(
                maintainer_ai_config_id=int(maintainer.id),
                reporter_ai_config_id=ai_config_id, device_id=device.device_id,
                title=str(args.get("title") or ""), description=str(args.get("description") or ""),
                acceptance_criteria=str(args.get("acceptance_criteria") or ""),
                affected_repo=str(args.get("affected_repo") or ""),
                source_session_id=str(args.get("source_session_id") or ""),
                severity=str(args.get("severity") or "normal"),
                dedupe_key=str(args.get("dedupe_key") or ""), deadline_at=args.get("deadline_at"),
            ))
        except (MaintenanceConflict, MaintenanceNotFound) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    delivered = await _dispatch(task, "codex:run_start", run_start_payload(task))
    waiting = None
    if not delivered:
        waiting = _record_waiting(task.task_id, f"run_start:{task.run_id}", "run_start", {})
    await sio.emit("maintenance:update", {"task_id": task.task_id, "task": task_dto(task)},
                   room=f"user_{user_id}")
    if waiting is not None:
        await sio.emit("maintenance:update", {
            "task_id": task.task_id, "event": event_dto(waiting),
        }, room=f"user_{user_id}")
    return {
        "task": task_dto(task),
        "delivery_status": "delivered" if delivered else "waiting_device",
    }


def _maintenance_status(user_id: int, args: dict, ai_config_id: Optional[int] = None):
    with Session(engine) as session:
        service = MaintenanceService(session)
        task_id = str(args.get("task_id") or "")
        if task_id:
            try:
                task = service.owned_task(user_id, task_id)
            except MaintenanceNotFound as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            events = session.exec(select(MaintenanceEvent).where(
                MaintenanceEvent.task_id == task_id,
            ).order_by(MaintenanceEvent.id.desc()).limit(20)).all()
            return {"task": task_dto(task), "events": [event_dto(row) for row in reversed(events)]}
        statement = select(MaintenanceTask).where(MaintenanceTask.user_id == user_id)
        status = str(args.get("status") or "")
        if status:
            statement = statement.where(MaintenanceTask.status == status)
        rows = session.exec(statement.order_by(MaintenanceTask.updated_at.desc()).limit(
            max(1, min(int(args.get("limit") or 20), 100))
        )).all()
        return {"items": [task_dto(row) for row in rows]}


async def _maintenance_comment(user_id: int, args: dict, ai_config_id: Optional[int] = None):
    request_id = str(args.get("request_id") or uuid.uuid4().hex)
    content = str(args.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content is required")
    with Session(engine) as session:
        service = MaintenanceService(session)
        try:
            task = service.owned_task(user_id, str(args.get("task_id") or ""), lock=True)
            existing = session.exec(select(MaintenanceEvent).where(
                MaintenanceEvent.run_id == task.run_id,
                MaintenanceEvent.event_id == f"cmd:{request_id}",
            )).first()
            if not existing and task.status in {"succeeded", "failed", "cancelled"}:
                raise MaintenanceConflict("terminal maintenance task cannot accept comments")
            event = service.append_event(task, EventRecord(
                "command.steer", "member",
                {"command_id": request_id, "command": "steer", "text": content},
                event_id=f"cmd:{request_id}", actor_id=str(ai_config_id or ""),
            ))
            session.commit()
            session.refresh(task)
            session.refresh(event)
        except (MaintenanceConflict, MaintenanceNotFound) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    stored = MaintenanceService.event_payload(event).get("payload") or {}
    command_payload = {key: value for key, value in stored.items() if key != "command"}
    delivered = await _dispatch(task, "codex:steer", command_payload)
    if not delivered:
        waiting = _record_waiting(task.task_id, request_id, "steer", command_payload)
        await sio.emit("maintenance:update", {
            "task_id": task.task_id, "event": event_dto(waiting),
        }, room=f"user_{user_id}")
    await sio.emit("maintenance:update", {
        "task_id": task.task_id, "task": task_dto(task), "event": event_dto(event),
    }, room=f"user_{user_id}")
    return {
        "ok": True, "accepted": True, "task_id": task.task_id,
        "event": event_dto(event),
        "delivery_status": "delivered" if delivered else "waiting_device",
    }
