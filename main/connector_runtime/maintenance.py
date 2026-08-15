"""Socket and internal dispatch contract for the Codex maintenance device."""

from __future__ import annotations

import logging
import json
from typing import Any, Optional

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlmodel import Session, select

from api.database import engine
from api.models import DeviceAiBinding
from api.models.maintenance import MaintenanceEvent, MaintenanceTask
from api.services.maintenance import (
    ApprovalRequestRecord, DeviceEventRecord, EventRecord,
    MaintenanceConflict, MaintenanceNotFound, MaintenanceService,
)
from api.sio import agents, sio, ui_sio


logger = logging.getLogger(__name__)
_COMMAND_EVENTS = {
    "command.steer": "codex:steer",
    "command.interrupt": "codex:interrupt",
    "command.approval_decision": "codex:approval_decision",
}


class MaintenanceCommandRequest(BaseModel):
    user_id: int
    task_id: str
    run_id: str
    device_id: str
    ai_config_id: int
    event: str
    payload: dict[str, Any] = Field(default_factory=dict)


class DeviceEventPayload(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    run_id: str = Field(alias="runId", min_length=1, max_length=200)
    event_id: str = Field(alias="eventId", min_length=1, max_length=200)
    sequence: int = Field(ge=1)
    phase: str = Field(default="", max_length=40)
    status: str = Field(default="", max_length=40)
    lease_seconds: int = Field(default=300, alias="leaseSeconds", ge=30, le=1800)
    payload: dict[str, Any] = Field(default_factory=dict)
    type: str = Field(default="", max_length=100)
    data: Any = None
    thread_id: str = Field(default="", alias="threadId", max_length=200)
    turn_id: str = Field(default="", alias="turnId", max_length=200)
    workspace: str = Field(default="", max_length=2000)
    branch: str = Field(default="", max_length=500)
    base_sha: str = Field(default="", alias="baseSha", max_length=100)
    summary: str = Field(default="", max_length=100_000)
    error: str = Field(default="", max_length=20_000)

    def event_payload(self) -> dict[str, Any]:
        value = dict(self.payload)
        if self.type:
            value["type"] = self.type
        if self.data is not None:
            value["data"] = self.data
        if self.thread_id:
            value["thread_id"] = self.thread_id
        if self.turn_id:
            value["turn_id"] = self.turn_id
        if self.branch:
            value["branch"] = self.branch
        if self.base_sha:
            value["baseSha"] = self.base_sha
        if self.workspace:
            value["workspace_attached"] = True
        if self.summary:
            value["summary"] = self.summary
        if self.error:
            value["error"] = self.error
        return value


class ApprovalRequestedPayload(DeviceEventPayload):
    approval_id: str = Field(alias="approvalId", min_length=1, max_length=200)
    approval_type: str = Field(default="", alias="approvalType", max_length=80)
    method: str = Field(default="", max_length=200)
    title: str = Field(default="", max_length=500)
    detail: dict[str, Any] = Field(default_factory=dict)
    request: dict[str, Any] = Field(default_factory=dict)
    expires_at: Optional[float] = Field(default=None, alias="expiresAt")


class CommandAckPayload(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    command_id: str = Field(alias="commandId", min_length=1, max_length=200)
    command: str = Field(default="", max_length=100)
    success: bool = True
    error: str = Field(default="", max_length=20_000)
    event_id: str = Field(default="", alias="eventId", max_length=200)
    run_id: str = Field(default="", alias="runId", max_length=200)
    sequence: Optional[int] = Field(default=None, ge=1)


def _device_context(sid: str) -> tuple[str, int, tuple[int, ...]]:
    agent = agents.get(sid)
    if not agent:
        raise MaintenanceNotFound("registered device socket not found")
    device_id = str(agent.get("id") or "")
    user_id = int(agent.get("userId") or 0)
    bound = tuple(int(value) for value in agent.get("boundAiConfigIds") or [] if int(value) > 0)
    platform = str(agent.get("platform") or "").strip().lower()
    if not device_id or not user_id or platform != "codex-maintainer":
        raise MaintenanceConflict("socket is not an authenticated codex-maintainer device")
    return device_id, user_id, bound


async def _emit_update(task: MaintenanceTask, event=None, approval=None) -> None:
    from api.services.maintenance.views import approval_dto, event_dto, task_dto

    payload: dict[str, Any] = {"task_id": task.task_id, "task": task_dto(task)}
    if event is not None:
        payload["event"] = event_dto(event)
    if approval is not None:
        payload["approval"] = approval_dto(approval)
    await ui_sio.emit("maintenance:update", payload, room=f"user_{task.user_id}")


def _validated(model, raw: object):
    return model.model_validate(raw if isinstance(raw, dict) else {})


async def _record(sid: str, raw: object, event_type: str, *, forced_status: str = "") -> dict:
    data = _validated(DeviceEventPayload, raw)
    device_id, user_id, bound = _device_context(sid)
    with Session(engine) as session:
        service = MaintenanceService(session)
        task = session.exec(select(MaintenanceTask).where(
            MaintenanceTask.run_id == data.run_id, MaintenanceTask.user_id == user_id,
        )).first()
        if not task or task.maintainer_ai_config_id not in bound:
            raise MaintenanceNotFound("maintenance run is not bound to this device member")
        stored_type = data.type if event_type == "run.event" and data.type else event_type
        task, event = service.device_event(user_id, DeviceEventRecord(
            device_id=device_id, run_id=data.run_id, event_id=data.event_id,
            sequence=data.sequence, event_type=stored_type, payload=data.event_payload(),
            status=forced_status or data.status, phase=data.phase,
            lease_seconds=data.lease_seconds,
        ))
    await _emit_update(task, event=event)
    return {"ok": True, "event_id": event.event_id, "sequence": event.sequence}


async def run_started(sid: str, raw: object) -> dict:
    return await _record(sid, raw, "run.started", forced_status="running")


async def event(sid: str, raw: object) -> dict:
    return await _record(sid, raw, "run.event")


async def command_ack(sid: str, raw: object) -> dict:
    data = _validated(CommandAckPayload, raw)
    device_id, user_id, bound = _device_context(sid)
    with Session(engine) as session:
        service = MaintenanceService(session)
        statement = select(MaintenanceEvent).where(
            MaintenanceEvent.event_id == f"cmd:{data.command_id}",
            MaintenanceEvent.actor_type.in_(["user", "member", "controller"]),
        )
        if data.run_id:
            statement = statement.where(MaintenanceEvent.run_id == data.run_id)
        command = session.exec(statement).first()
        if not command:
            raise MaintenanceNotFound("maintenance command not found")
        task = service.owned_task(user_id, command.task_id, lock=True)
        if task.device_id != device_id or task.maintainer_ai_config_id not in bound:
            raise MaintenanceNotFound("maintenance command is not assigned to this device")
        event_row = service.append_event(task, EventRecord(
            "command.acknowledged", "device",
            {"command_id": data.command_id, "command": data.command,
             "success": data.success, "error": data.error},
            event_id=f"ack:{data.command_id}", actor_id=device_id,
        ))
        session.commit()
        session.refresh(task)
        session.refresh(event_row)
    await _emit_update(task, event=event_row)
    return {"ok": True, "command_id": data.command_id, "event_id": event_row.event_id}


async def run_completed(sid: str, raw: object) -> dict:
    data = _validated(DeviceEventPayload, raw)
    status = {"completed": "succeeded", "interrupted": "cancelled", "failed": "failed"}.get(
        data.status, data.status
    )
    if status not in {"succeeded", "failed", "cancelled"}:
        raise MaintenanceConflict("run completion requires a terminal status")
    normalized = data.model_dump(by_alias=True)
    normalized["status"] = status
    response = await _record(sid, normalized, "run.completed")
    with Session(engine) as session:
        task = session.exec(select(MaintenanceTask).where(
            MaintenanceTask.run_id == data.run_id,
        )).first()
    if task is not None:
        from connector_runtime.maintenance_conversation_bridge import complete_conversation_task

        complete_conversation_task(task)
    return response


async def approval_requested(sid: str, raw: object) -> dict:
    data = _validated(ApprovalRequestedPayload, raw)
    device_id, user_id, bound = _device_context(sid)
    with Session(engine) as session:
        service = MaintenanceService(session)
        task = session.exec(select(MaintenanceTask).where(
            MaintenanceTask.run_id == data.run_id, MaintenanceTask.user_id == user_id,
        ).with_for_update()).first()
        if not task or task.device_id != device_id or task.maintainer_ai_config_id not in bound:
            raise MaintenanceNotFound("maintenance run is not assigned to this device")
        approval = service.request_approval(task, ApprovalRequestRecord(
            event_id=data.event_id, sequence=data.sequence, approval_id=data.approval_id,
            approval_type=data.approval_type or data.method or "command",
            title=data.title or data.method, detail=data.detail or data.request,
            expires_at=data.expires_at,
        ))
        event_row = session.exec(select(MaintenanceEvent).where(
            MaintenanceEvent.run_id == task.run_id,
            MaintenanceEvent.event_id == data.event_id,
        )).first()
        session.refresh(task)
        session.refresh(approval)
        if event_row is not None:
            session.refresh(event_row)
    await _emit_update(task, event=event_row, approval=approval)
    return {"ok": True, "approval_id": approval.approval_id}


def _live_sid(request: MaintenanceCommandRequest) -> str:
    for sid, agent in list(agents.items()):
        if str(agent.get("id") or "") != request.device_id:
            continue
        if int(agent.get("userId") or 0) != request.user_id:
            continue
        if str(agent.get("platform") or "").strip().lower() != "codex-maintainer":
            continue
        bound = {int(value) for value in agent.get("boundAiConfigIds") or []}
        if request.ai_config_id in bound:
            return sid
    raise HTTPException(status_code=503, detail="codex-maintainer device is offline")


def _event_data(row: MaintenanceEvent) -> dict:
    try:
        value = json.loads(row.payload_json or "{}")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def pending_commands(rows: list[MaintenanceEvent]) -> list[tuple[str, dict]]:
    acked = {
        str(_event_data(row).get("command_id") or "")
        for row in rows if row.event_type == "command.acknowledged"
    }
    pending: list[tuple[str, dict]] = []
    for row in sorted(rows, key=lambda item: item.sequence):
        socket_event = _COMMAND_EVENTS.get(row.event_type)
        payload = _event_data(row)
        command_id = str(payload.get("command_id") or "")
        if not socket_event or not command_id or command_id in acked:
            continue
        mapped = {
            {"command_id": "commandId", "approval_id": "approvalId"}.get(key, key): value
            for key, value in payload.items() if key != "command"
        }
        pending.append((socket_event, mapped))
    return pending


async def send_command(request: MaintenanceCommandRequest) -> dict:
    if request.event not in {
        "codex:run_start", "codex:steer", "codex:interrupt", "codex:approval_decision",
    }:
        raise HTTPException(status_code=400, detail="unsupported maintenance command")
    with Session(engine) as session:
        task = session.exec(select(MaintenanceTask).where(
            MaintenanceTask.task_id == request.task_id,
            MaintenanceTask.run_id == request.run_id,
            MaintenanceTask.user_id == request.user_id,
            MaintenanceTask.device_id == request.device_id,
            MaintenanceTask.maintainer_ai_config_id == request.ai_config_id,
        )).first()
        binding = session.exec(select(DeviceAiBinding).where(
            DeviceAiBinding.user_id == request.user_id,
            DeviceAiBinding.device_id == request.device_id,
            DeviceAiBinding.ai_config_id == request.ai_config_id,
        )).first()
    if not task or not binding:
        raise HTTPException(status_code=403, detail="maintenance command ownership mismatch")
    sid = _live_sid(request)
    mapped = {
        {"command_id": "commandId", "approval_id": "approvalId"}.get(key, key): value
        for key, value in request.payload.items()
    }
    payload = {"taskId": task.task_id, "runId": task.run_id,
               "lastSequence": task.last_sequence,
               "lastDeviceSequence": task.last_device_sequence, **mapped}
    await sio.emit(request.event, payload, to=sid)
    return {"ok": True, "delivered": True, "run_id": task.run_id}


async def resume_codex_maintenance(device_id: str, user_id: int, ai_config_ids: tuple[int, ...]) -> None:
    with Session(engine) as session:
        rows = session.exec(select(MaintenanceTask).where(
            MaintenanceTask.user_id == user_id, MaintenanceTask.device_id == device_id,
            MaintenanceTask.maintainer_ai_config_id.in_(list(ai_config_ids)),
        ).order_by(MaintenanceTask.created_at.asc())).all()
        events = session.exec(select(MaintenanceEvent).where(
            MaintenanceEvent.task_id.in_([row.task_id for row in rows]),
        ).order_by(MaintenanceEvent.sequence.asc())).all() if rows else []
    sid = next((sid for sid, agent in agents.items() if str(agent.get("id") or "") == device_id), None)
    if not sid:
        return
    from api.services.maintenance.views import run_start_payload
    for task in rows:
        if task.status not in {"queued", "running"}:
            continue
        payload = run_start_payload(task)
        payload.update({"taskId": task.task_id, "runId": task.run_id,
                        "commandId": f"run_start:{task.run_id}",
                        "resume": task.status == "running"})
        await sio.emit("codex:run_start", payload, to=sid)
        task_events = [row for row in events if row.task_id == task.task_id]
        for socket_event, command in pending_commands(task_events):
            await sio.emit(socket_event, {"taskId": task.task_id, "runId": task.run_id,
                                          **command}, to=sid)


async def guarded(handler, sid: str, raw: object) -> dict:
    try:
        return await handler(sid, raw)
    except ValidationError:
        return {"ok": False, "error_code": "PAYLOAD_INVALID"}
    except MaintenanceNotFound:
        return {"ok": False, "error_code": "RUN_NOT_FOUND"}
    except MaintenanceConflict as exc:
        return {"ok": False, "error_code": "STATE_CONFLICT", "detail": str(exc)}
    except Exception:
        logger.exception("codex maintenance socket handler failed")
        return {"ok": False, "error_code": "INTERNAL_ERROR"}
