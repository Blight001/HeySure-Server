"""First-party Codex device ownership for legacy external-controller chat turns."""

from __future__ import annotations

import time
from typing import Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from api.models import ChatMessage, ChatMessageCreate
from api.models.external_control import ExternalControllerTurn
from api.models.maintenance import MaintenanceTask
from api.services.chat.chat_persistence import _save_message

from .state import transition_turn


DEVICE_LEASE_PREFIX = "codex-device:"


def turn_context(session: Session, row: ExternalControllerTurn, limit: int = 30) -> dict:
    messages = session.exec(select(ChatMessage).where(
        ChatMessage.user_id == row.user_id,
        ChatMessage.ai_config_id == row.ai_config_id,
        ChatMessage.ai_kind == row.ai_kind,
        ChatMessage.session_id == row.session_id,
        ChatMessage.id <= row.user_message_id,
    ).order_by(ChatMessage.id.desc()).limit(max(1, min(int(limit), 50)))).all()
    user_message = session.get(ChatMessage, row.user_message_id)
    return {
        "turn_id": row.turn_id,
        "session_id": row.session_id,
        "session_name": row.session_name,
        "content": str(user_message.content if user_message else ""),
        "history": [
            {"role": item.role, "content": str(item.content or "")[:20_000]}
            for item in reversed(messages)
        ],
    }


def claim_for_device(
    session: Session, turn_id: str, device_id: str, *, lease_seconds: int = 1800
) -> Optional[ExternalControllerTurn]:
    row = session.exec(select(ExternalControllerTurn).where(
        ExternalControllerTurn.turn_id == turn_id,
    ).with_for_update()).first()
    if row is None or row.status != "queued":
        return None
    now = time.time()
    transition_turn(row, "running", now)
    row.lease_owner = f"{DEVICE_LEASE_PREFIX}{device_id}"[:200]
    row.lease_expires_at = now + max(60, min(int(lease_seconds), 7200))
    row.attempt += 1
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def complete_from_device(
    session: Session,
    turn_id: str,
    device_id: str,
    *,
    status: str,
    content: str = "",
    error: str = "",
) -> ExternalControllerTurn:
    row = session.exec(select(ExternalControllerTurn).where(
        ExternalControllerTurn.turn_id == turn_id,
    ).with_for_update()).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Conversation turn not found")
    expected_owner = f"{DEVICE_LEASE_PREFIX}{device_id}"
    if row.status != "running" or row.lease_owner != expected_owner:
        raise HTTPException(status_code=409, detail="Conversation turn is not owned by this device")
    if status == "succeeded":
        body = str(content or "").strip()
        if not body:
            body = "Codex 已完成处理，但没有返回可展示的文本摘要。"
        message = _save_message(session, row.user_id, ChatMessageCreate(
            role="assistant", content=body, model="codex-agent",
            finish_reason="stop", ai_config_id=row.ai_config_id,
            ai_kind=row.ai_kind, session_id=row.session_id,
            session_name=row.session_name,
        ))
        transition_turn(row, "succeeded")
        row.assistant_message_id = int(message.id or 0)
    else:
        transition_turn(row, "failed")
        row.error_message = str(error or f"codex_agent finished with {status}")[:4_000]
    row.lease_expires_at = None
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def requeue_orphaned_device_turns(session: Session) -> int:
    rows = session.exec(select(ExternalControllerTurn).where(
        ExternalControllerTurn.status == "running",
        ExternalControllerTurn.lease_owner.startswith(DEVICE_LEASE_PREFIX),
    )).all()
    changed = 0
    for row in rows:
        active_task = session.exec(select(MaintenanceTask).where(
            MaintenanceTask.user_id == row.user_id,
            MaintenanceTask.dedupe_key == f"external_turn:{row.turn_id}",
            MaintenanceTask.status.in_(["queued", "running", "waiting_user"]),
        )).first()
        if active_task is not None:
            continue
        if row.attempt >= 3:
            continue
        transition_turn(row, "queued")
        session.add(row)
        changed += 1
    if changed:
        session.commit()
    return changed
