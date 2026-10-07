"""Inbound Codex bridge events from a Windows device."""

from __future__ import annotations

import time
from typing import Any

from sqlmodel import Session, select

from api.database import engine
from api.models import ChatMessageCreate, ChatRun, CodexSession
from api.services.chat.chat_persistence import _save_message
from api.services.codex_bridge import append_event, set_device_models
from api.sio import agents, ui_sio


_CODEX_CHAT_TEXT: dict[str, str] = {}
_CODEX_CHAT_DELTA_SEEN: set[str] = set()


def _agent(sid: str) -> dict | None:
    value = agents.get(sid)
    return value if isinstance(value, dict) else None


async def models(sid: str, data: object) -> None:
    agent = _agent(sid)
    if not agent or not isinstance(data, dict):
        return
    user_id = agent.get("userId")
    device_id = str(agent.get("id") or "")
    if user_id is None or not device_id:
        return
    items = data.get("models") if isinstance(data.get("models"), list) else []
    normalized = set_device_models(int(user_id), device_id, items)
    agent["codexModels"] = normalized
    await ui_sio.emit("codex:models", {"deviceId": device_id, "models": normalized}, room=f"user_{int(user_id)}")


async def event(sid: str, data: object) -> None:
    agent = _agent(sid)
    if not agent or not isinstance(data, dict):
        return
    session_id = str(data.get("sessionId") or data.get("requestId") or "").strip()
    if not session_id or agent.get("userId") is None:
        return
    from api.services.codex_bridge import get_session

    row = get_session(int(agent["userId"]), session_id)
    if row is None or str(row.device_id) != str(agent.get("id") or ""):
        return
    payload = data.get("payload") if "payload" in data else data.get("event", data)
    if isinstance(payload, dict):
        nested = payload.get("data")
        nested_type = nested.get("type") if isinstance(nested, dict) else ""
        event_type = str(
            payload.get("type") or nested_type or data.get("type")
            or data.get("eventType") or "event"
        )[:120]
    else:
        event_type = str(data.get("type") or data.get("eventType") or "event")[:120]
    sequence = data.get("sequence")
    record = append_event(user_id=int(agent["userId"]), session_id=session_id, event_type=event_type, payload=payload, sequence=sequence)
    if record is None:
        return
    await ui_sio.emit("codex:event", {"sessionId": session_id, "sequence": record.sequence, "type": event_type, "payload": payload}, room=f"user_{int(agent['userId'])}")
    await _update_chat_run(session_id, int(agent["userId"]), event_type, payload)


def _text_from_payload(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    for key in ("text", "content", "output", "message", "delta", "text_delta", "value"):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item
    nested = value.get("data") or value.get("item") or value.get("delta")
    return _text_from_payload(nested)


def _event_text(session_id: str, event_type: str, payload: Any) -> str:
    """Prefer deltas and avoid appending item.completed snapshots twice.

    ``codex exec --json`` normally emits ``item.delta`` followed by an
    ``item.completed`` record containing the same final text.  Persisting both
    would duplicate every assistant response.  If a bridge only emits the
    completed record, it is still accepted as the fallback text.
    """
    lowered = str(event_type or "").lower()
    if "delta" in lowered:
        _CODEX_CHAT_DELTA_SEEN.add(session_id)
        return _text_from_payload(payload)
    if lowered in {"item.completed", "message.completed", "agent_message.completed"}:
        if session_id in _CODEX_CHAT_DELTA_SEEN:
            return ""
        return _text_from_payload(payload)
    return _text_from_payload(payload) if lowered in {"agent_message", "assistant.message"} else ""


async def _update_chat_run(session_id: str, user_id: int, event_type: str, payload: Any) -> None:
    """Mirror Codex events into the existing ChatRun/UI stream when session_id
    is a run id. Standalone /api/codex sessions remain Codex-event-only."""
    with Session(engine) as session:
        row = session.exec(select(ChatRun).where(
            ChatRun.run_id == session_id, ChatRun.user_id == user_id,
        )).first()
        if row is None:
            return
        text = _event_text(session_id, event_type, payload)
        if text:
            _CODEX_CHAT_TEXT[session_id] = _CODEX_CHAT_TEXT.get(session_id, "") + text
        now = time.time()
        if event_type == "turn.started":
            row.status = "running"
            row.started_at = row.started_at or now
        elif event_type in {"turn.completed", "turn.failed", "turn.interrupted"}:
            terminal = "completed" if event_type == "turn.completed" else ("stopped" if event_type == "turn.interrupted" else "error")
            if row.stop_requested and terminal == "completed":
                terminal = "stopped"
            row.status = terminal
            row.error_message = "" if terminal == "completed" else _text_from_payload(payload)
            row.finished_at = now
        row.updated_at = now
        session.add(row)
        session.commit()
        snapshot = {
            "run_id": row.run_id, "user_id": user_id, "status": row.status,
            "session_id": row.session_id, "ai_config_id": row.ai_config_id,
            "ai_kind": row.ai_kind, "text": _CODEX_CHAT_TEXT.get(session_id, ""),
            "phase": "generating" if row.status in {"queued", "running"} else "idle",
            "updated_at": now, "codex_event": event_type,
        }
        if row.status in {"completed", "error", "stopped"}:
            text_value = _CODEX_CHAT_TEXT.pop(session_id, "")
            _CODEX_CHAT_DELTA_SEEN.discard(session_id)
            if text_value and row.status == "completed":
                codex_row = session.get(CodexSession, session_id)
                _save_message(session, user_id, ChatMessageCreate(
                    role="assistant", content=text_value,
                    model=(codex_row.model if codex_row else "codex_local"),
                    ai_config_id=row.ai_config_id, ai_kind=row.ai_kind,
                    session_id=row.session_id, session_name=row.session_name,
                ))
            done = {key: snapshot[key] for key in (
                "run_id", "user_id", "status", "session_id", "ai_config_id", "ai_kind",
            )}
            await ui_sio.emit("chat:run_done", done, room=f"user_{user_id}")
            return
    await ui_sio.emit("chat:run_live", snapshot, room=f"user_{user_id}")


async def started(sid: str, data: object) -> None:
    await event(sid, {**(data if isinstance(data, dict) else {}), "type": "session.started"})

