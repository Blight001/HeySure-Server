"""Persistent MCP operation recording used to draft reproducible cards."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, NamedTuple, Optional

from sqlmodel import Session, select
from sqlalchemy.exc import IntegrityError

from api.models import WorkflowRecording, WorkflowRecordingEvent


SENSITIVE_KEYS = {"authorization", "cookie", "password", "secret", "token", "api_key", "apikey"}
MAX_RECORDED_BYTES = 64 * 1024


class RecordedToolCall(NamedTuple):
    user_id: int
    ai_config_id: Optional[int]
    tool: str
    arguments: Dict[str, Any]
    result: Any
    success: bool
    error: str
    device_id: str


def _dump(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text.encode("utf-8")) <= MAX_RECORDED_BYTES:
        return text
    return json.dumps({"truncated": True, "preview": text[:16000]}, ensure_ascii=False)


def _load(raw: str, fallback: Any) -> Any:
    try:
        return json.loads(raw or "")
    except Exception:
        return fallback


def _redact(value: Any, depth: int = 0) -> Any:
    if depth > 12:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if str(key).lower() in SENSITIVE_KEYS else _redact(child, depth + 1)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact(child, depth + 1) for child in value[:200]]
    return value


def _owner_statement(user_id: int, ai_config_id: Optional[int]):
    statement = select(WorkflowRecording).where(
        WorkflowRecording.user_id == user_id,
        WorkflowRecording.status == "active",
    )
    return statement.where(
        WorkflowRecording.ai_config_id == ai_config_id
        if ai_config_id is not None
        else WorkflowRecording.ai_config_id.is_(None)
    )


def active_recording(
    session: Session, user_id: int, ai_config_id: Optional[int], *, lock: bool = False
) -> Optional[WorkflowRecording]:
    statement = _owner_statement(user_id, ai_config_id).order_by(WorkflowRecording.created_at.desc())
    if lock:
        statement = statement.with_for_update()
    return session.exec(statement).first()


def recording_payload(session: Session, row: WorkflowRecording, *, include_events: bool = False) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "id": row.id,
        "status": row.status,
        "name": row.name,
        "description": row.description,
        "default_device_id": row.default_device_id,
        "device_ids": _load(row.device_ids_json, []),
        "event_count": row.event_count,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "stopped_at": row.stopped_at,
    }
    if include_events:
        events = session.exec(select(WorkflowRecordingEvent).where(
            WorkflowRecordingEvent.recording_id == row.id,
        ).order_by(WorkflowRecordingEvent.sequence)).all()
        payload["calls"] = [
            {
                "sequence": item.sequence,
                "tool": item.tool_name,
                "device_id": item.device_id,
                "arguments": _load(item.arguments_json, {}),
                "result": _load(item.result_json, {}),
                "success": item.success,
                "error": item.error,
            }
            for item in events
        ]
    return payload


def start_recording(
    session: Session,
    *,
    user_id: int,
    ai_config_id: Optional[int],
    name: str,
    description: str,
    default_device_id: str,
    device_ids: list[str],
) -> WorkflowRecording:
    current = active_recording(session, user_id, ai_config_id, lock=True)
    if current:
        return current
    selected = list(dict.fromkeys(str(item).strip() for item in device_ids if str(item).strip()))
    default = str(default_device_id or "").strip() or (selected[0] if selected else "")
    if default and default not in selected:
        selected.insert(0, default)
    now = time.time()
    row = WorkflowRecording(
        id=f"wrec_{uuid.uuid4().hex}",
        user_id=user_id,
        ai_config_id=ai_config_id,
        name=str(name or "操作录制")[:160],
        description=str(description or "")[:4000],
        default_device_id=default,
        device_ids_json=_dump(selected),
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        current = active_recording(session, user_id, ai_config_id)
        if current:
            return current
        raise
    session.refresh(row)
    return row


def record_completed_tool_call(
    session: Session,
    call: RecordedToolCall,
) -> None:
    if call.tool == "automation.manage":
        return
    row = active_recording(session, call.user_id, call.ai_config_id, lock=True)
    if not row:
        return
    selected = _load(row.device_ids_json, [])
    if call.device_id and call.device_id not in selected:
        selected.append(call.device_id)
        row.device_ids_json = _dump(selected)
        if not row.default_device_id:
            row.default_device_id = call.device_id
    row.event_count += 1
    row.updated_at = time.time()
    event = WorkflowRecordingEvent(
        id=f"wrecevt_{uuid.uuid4().hex}",
        recording_id=row.id,
        sequence=row.event_count,
        tool_name=str(call.tool),
        device_id=str(call.device_id or ""),
        arguments_json=_dump(_redact(call.arguments)),
        result_json=_dump(_redact(call.result)),
        success=bool(call.success),
        error=str(call.error or "")[:4000],
    )
    session.add(row)
    session.add(event)
    session.commit()


def stop_recording(
    session: Session, user_id: int, ai_config_id: Optional[int], *, cancel: bool = False
) -> Optional[WorkflowRecording]:
    row = active_recording(session, user_id, ai_config_id, lock=True)
    if not row:
        return None
    now = time.time()
    row.status = "cancelled" if cancel else "stopped"
    row.stopped_at = now
    row.updated_at = now
    session.add(row)
    session.commit()
    session.refresh(row)
    return row
