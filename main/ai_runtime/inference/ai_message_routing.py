"""Session route resolution for AI-to-AI messages."""
from __future__ import annotations

import time
from typing import Any, Dict, Optional
from sqlmodel import Session, select
from api.database import engine
from api.models import AIMessage, ChatRun
from .ai_message_state import _pending_replies
from .ai_message_store import _row_to_dict

def get_active_session_id(user_id: int, to_ai_config_id: int) -> Optional[str]:
    """返回目标 AI 当前最新活跃 run 的 session_id；无则 None。"""
    with Session(engine) as session:
        row = _get_live_active_run(session, user_id, to_ai_config_id)
        return row.session_id if row else None


def find_corresponding_target_session_id(
    *,
    user_id: int,
    from_ai_config_id: int,
    to_ai_config_id: int,
    from_session_id: str,
) -> str:
    """Return the target-side session bound to this sender conversation."""
    from_session_id = (from_session_id or "").strip()
    if not from_session_id:
        return ""
    with Session(engine) as session:
        row = session.exec(
            select(AIMessage).where(
                AIMessage.user_id == user_id,
                AIMessage.from_ai_config_id == from_ai_config_id,
                AIMessage.to_ai_config_id == to_ai_config_id,
                AIMessage.from_session_id == from_session_id,
                AIMessage.target_session_id != "",
                AIMessage.status != "failed",
            ).order_by(AIMessage.created_at.desc())
        ).first()
        if row:
            target_session_id = str(row.target_session_id or "").strip()
            if target_session_id:
                return target_session_id
    return stable_peer_session_id(
        user_id=user_id,
        from_ai_config_id=from_ai_config_id,
        to_ai_config_id=to_ai_config_id,
        from_session_id=from_session_id,
    )


def find_reverse_inbound_session(
    *,
    user_id: int,
    current_ai_config_id: int,
    target_ai_config_id: int,
) -> str:
    """Pick the target AI's own conversation to route a fresh outbound message into.

    When the current AI reaches out to ``target_ai_config_id`` *outside* any live
    same-session reply route — e.g. B independently found the cause of A's failed
    task and now wants to notify A, from a different session than the one where B
    received A's original notice — we still want the message to land back in A's
    original conversation instead of minting a brand-new isolated session on A's
    side.

    Look up the most recent message in the REVERSE direction (one the target A
    previously sent to the current AI B) and reuse *its* ``from_session_id`` —
    A's own conversation at that time. Returns "" when the two AIs have never
    talked, so genuinely-new conversations fall through to the normal path.
    """
    with Session(engine) as session:
        row = session.exec(
            select(AIMessage).where(
                AIMessage.user_id == user_id,
                AIMessage.from_ai_config_id == target_ai_config_id,
                AIMessage.to_ai_config_id == current_ai_config_id,
                AIMessage.from_session_id != "",
                AIMessage.status != "failed",
            ).order_by(AIMessage.created_at.desc())
        ).first()
        if row:
            return str(row.from_session_id or "").strip()
    return ""


def find_return_route(
    *,
    user_id: int,
    current_ai_config_id: int,
    target_ai_config_id: int,
    current_session_id: str,
) -> Dict[str, Any]:
    """Find the original sender session when replying with message.send+to.

    If AI B is currently processing a message from AI A in session S2, the
    original AIMessage stores A's session as ``from_session_id``. A later
    ``message.send+to(to_ai_config_id=A)`` from S2 should route back there.
    """
    current_session_id = (current_session_id or "").strip()
    if not current_session_id:
        return {}
    with Session(engine) as session:
        row = session.exec(
            select(AIMessage).where(
                AIMessage.user_id == user_id,
                AIMessage.from_ai_config_id == target_ai_config_id,
                AIMessage.to_ai_config_id == current_ai_config_id,
                AIMessage.target_session_id == current_session_id,
                AIMessage.from_session_id != "",
                AIMessage.status.in_(["delivered", "replied", "timeout"]),
            ).order_by(AIMessage.delivered_at.desc(), AIMessage.created_at.desc())
        ).first()
        return _row_to_dict(row) if row else {}


def find_return_route_by_message_id(
    *,
    user_id: int,
    current_ai_config_id: int,
    target_ai_config_id: int,
    message_id: str,
) -> Dict[str, Any]:
    """Find the original route for an explicit AI message id."""
    message_id = (message_id or "").strip()
    if not message_id:
        return {}
    with Session(engine) as session:
        row = session.exec(
            select(AIMessage).where(
                AIMessage.user_id == user_id,
                AIMessage.message_id == message_id,
                AIMessage.from_ai_config_id == target_ai_config_id,
                AIMessage.to_ai_config_id == current_ai_config_id,
                AIMessage.from_session_id != "",
                AIMessage.status != "failed",
            )
        ).first()
        return _row_to_dict(row) if row else {}


def resolve_waiting_reply_to_message_id_from_send_message(
    *,
    user_id: int,
    current_ai_config_id: int,
    target_ai_config_id: int,
    message_id: str,
    content: str,
) -> Optional[Dict[str, Any]]:
    """Treat ``message.send+to`` as a reply to an explicit AI message id."""
    message_id = (message_id or "").strip()
    content = (content or "").strip()
    if not message_id or not content:
        return None
    with Session(engine) as session:
        row = session.exec(
            select(AIMessage).where(
                AIMessage.user_id == user_id,
                AIMessage.message_id == message_id,
                AIMessage.from_ai_config_id == target_ai_config_id,
                AIMessage.to_ai_config_id == current_ai_config_id,
                AIMessage.status.in_(["pending", "delivered", "timeout"]),
            )
        ).first()
        if not row:
            return None
        row.reply_content = content
        row.status = "replied"
        row.replied_at = time.time()
        session.add(row)
        session.commit()
        session.refresh(row)
        payload = _row_to_dict(row)
    resolved_waiter = _pending_replies.resolve(message_id, payload)
    payload["waiter_resolved"] = resolved_waiter
    payload["reply_to_message_id"] = message_id
    if not resolved_waiter:
        _enqueue_unwaited_reply(payload)
    return payload


def resolve_waiting_reply_from_send_message(
    *,
    user_id: int,
    current_ai_config_id: int,
    target_ai_config_id: int,
    current_session_id: str,
    content: str,
) -> Optional[Dict[str, Any]]:
    """Treat a return ``message.send+to`` as the reply for a waiting sender.

    Prompts now tell AIs to use ``message.send+to`` in both directions. This
    bridges that behavior with the older synchronous ``require_reply=true``
    wait path so the sender does not block until timeout.
    """
    content = (content or "").strip()
    current_session_id = (current_session_id or "").strip()
    if not content or not current_session_id:
        return None
    with Session(engine) as session:
        row = session.exec(
            select(AIMessage).where(
                AIMessage.user_id == user_id,
                AIMessage.from_ai_config_id == target_ai_config_id,
                AIMessage.to_ai_config_id == current_ai_config_id,
                AIMessage.target_session_id == current_session_id,
                AIMessage.from_session_id != "",
                AIMessage.status.in_(["pending", "delivered", "timeout"]),
            ).order_by(AIMessage.delivered_at.desc(), AIMessage.created_at.desc())
        ).first()
        if not row:
            return None
        row.reply_content = content
        row.status = "replied"
        row.replied_at = time.time()
        session.add(row)
        session.commit()
        session.refresh(row)
        payload = _row_to_dict(row)
    resolved_waiter = _pending_replies.resolve(str(payload.get("message_id") or ""), payload)
    payload["waiter_resolved"] = resolved_waiter
    payload["reply_to_message_id"] = payload.get("message_id")
    if not resolved_waiter:
        _enqueue_unwaited_reply(payload)
    return payload


