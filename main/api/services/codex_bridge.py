"""Server-side coordination for the Windows local Codex bridge.

The bridge owns Codex credentials and the app-server process.  This module
only stores the device-reported catalogue, validates ownership and journals
opaque app-server events for the web UI.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Iterable

from sqlmodel import Session, select

from api.database import engine
from api.models import CodexEvent, CodexSession, DevicePresence


def normalize_models(items: Iterable[Any] | None) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for raw in items or ():
        if not isinstance(raw, dict):
            continue
        model_id = str(raw.get("id") or raw.get("model") or raw.get("name") or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        out.append({
            "id": model_id,
            "name": str(raw.get("name") or model_id).strip()[:200],
            "displayName": str(raw.get("displayName") or raw.get("display_name") or raw.get("name") or model_id).strip()[:200],
            "provider": str(raw.get("provider") or "codex").strip()[:80],
            "reasoning": raw.get("reasoning") if isinstance(raw.get("reasoning"), (dict, list, str, bool, int, float)) else None,
        })
    return out[:256]


def set_device_models(user_id: int, device_id: str, models: Iterable[Any] | None) -> list[dict]:
    normalized = normalize_models(models)
    with Session(engine) as session:
        row = session.exec(select(DevicePresence).where(
            DevicePresence.user_id == int(user_id), DevicePresence.device_id == str(device_id)
        ).order_by(DevicePresence.updated_at.desc(), DevicePresence.id.desc())).first()
        if row is None:
            return normalized
        row.codex_models_json = json.dumps(normalized, ensure_ascii=False)
        row.updated_at = time.time()
        session.add(row)
        session.commit()
    return normalized


def models_for_user(user_id: int) -> list[dict]:
    with Session(engine) as session:
        rows = session.exec(select(DevicePresence).where(DevicePresence.user_id == int(user_id))).all()
    result: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        try:
            models = json.loads(row.codex_models_json or "[]")
        except Exception:
            models = []
        for model in normalize_models(models):
            key = (str(row.device_id), model["id"])
            if key in seen:
                continue
            seen.add(key)
            result.append({
                **model,
                "deviceId": str(row.device_id),
                "deviceName": str(row.name or row.device_id),
                "devicePlatform": str(row.platform or ""),
                "deviceType": str(row.device_type or ""),
                "online": bool(row.online),
            })
    return result


def create_session(*, user_id: int, device_id: str, model: str, cwd: str = "",
                   ai_config_id: int | None = None, sandbox_policy: str = "",
                   approval_policy: str = "", session_id: str | None = None) -> CodexSession:
    row = CodexSession(
        session_id=str(session_id or uuid.uuid4().hex),
        user_id=int(user_id), device_id=str(device_id), model=str(model or ""),
        cwd=str(cwd or ""), ai_config_id=ai_config_id,
        sandbox_policy=str(sandbox_policy or ""), approval_policy=str(approval_policy or ""),
    )
    with Session(engine) as session:
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def get_session(user_id: int, session_id: str) -> CodexSession | None:
    with Session(engine) as session:
        row = session.get(CodexSession, str(session_id))
        return row if row and row.user_id == int(user_id) else None


def sessions_for_user(user_id: int) -> list[dict]:
    with Session(engine) as session:
        rows = session.exec(select(CodexSession).where(
            CodexSession.user_id == int(user_id)
        ).order_by(CodexSession.updated_at.desc())).all()
    return [{
        "sessionId": row.session_id, "deviceId": row.device_id,
        "threadId": row.codex_thread_id, "model": row.model,
        "cwd": row.cwd, "status": row.status, "createdAt": row.created_at,
        "updatedAt": row.updated_at,
    } for row in rows]


def append_event(*, user_id: int, session_id: str, event_type: str, payload: Any,
                 sequence: int | None = None) -> CodexEvent | None:
    row = get_session(user_id, session_id)
    if row is None:
        return None
    with Session(engine) as session:
        if sequence is None:
            latest = session.exec(select(CodexEvent).where(
                CodexEvent.session_id == str(session_id)
            ).order_by(CodexEvent.sequence.desc(), CodexEvent.id.desc())).first()
            sequence = int(latest.sequence + 1) if latest else 1
        event = CodexEvent(
            session_id=str(session_id), user_id=int(user_id), event_type=str(event_type or "event"),
            sequence=max(0, int(sequence)),
            payload_json=json.dumps(payload, ensure_ascii=False, default=str),
        )
        session.add(event)
        row = session.get(CodexSession, str(session_id))
        if row:
            row.updated_at = time.time()
            if event_type in {"turn.started", "turn.completed", "turn.failed", "turn.interrupted"}:
                row.status = event_type.split(".", 1)[1]
            session.add(row)
        session.commit()
        session.refresh(event)
    return event


def events_for_session(*, user_id: int, session_id: str, after: int = 0) -> list[dict]:
    if get_session(user_id, session_id) is None:
        return []
    with Session(engine) as session:
        rows = session.exec(select(CodexEvent).where(
            CodexEvent.session_id == str(session_id), CodexEvent.user_id == int(user_id),
            CodexEvent.sequence > int(after),
        ).order_by(CodexEvent.sequence.asc(), CodexEvent.id.asc())).all()
    result = []
    for row in rows:
        try:
            payload = json.loads(row.payload_json or "{}")
        except Exception:
            payload = row.payload_json
        result.append({"id": row.id, "sequence": row.sequence, "type": row.event_type,
                       "payload": payload, "createdAt": row.created_at})
    return result

