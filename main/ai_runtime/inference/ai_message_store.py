"""Persistence operations for AI-to-AI messages."""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, Optional
from sqlmodel import Session, select
from api.database import engine
from api.models import AIMessage, AssistantAIConfig
from .ai_message_state import (
    _content_requests_response, _new_message_id, _normalize_message_type,
    _pending_replies, DEFAULT_REPLY_WAIT_SECONDS,
)

logger = logging.getLogger(__name__)

def send(**message: Any) -> AIMessage:
    """Persist an AI message; callers use the original keyword-only contract."""
    content = str(message.get("content") or "").strip()
    source_id = int(message["from_ai_config_id"])
    target_id = int(message["to_ai_config_id"])
    target_session_id = str(message.get("target_session_id") or "").strip()
    if not content:
        raise ValueError("content is required")
    if source_id == target_id:
        raise ValueError("cannot send message to self")
    if not target_session_id:
        raise ValueError("target_session_id is required")
    user_id = int(message["user_id"])
    with Session(engine) as session:
        ids = session.exec(select(AssistantAIConfig.id).where(
            AssistantAIConfig.user_id == user_id,
            AssistantAIConfig.id.in_([source_id, target_id]),
        )).all()
        if len(set(ids)) != 2:
            raise ValueError("source or target AI config not found")
        row = AIMessage(
            message_id=_new_message_id(), user_id=user_id,
            from_ai_config_id=source_id, to_ai_config_id=target_id,
            target_session_id=target_session_id,
            from_session_id=str(message.get("from_session_id") or "").strip(),
            content=content, require_reply=bool(message.get("require_reply", True)),
            timeout_seconds=max(5, int(message.get("timeout_seconds") or DEFAULT_REPLY_WAIT_SECONDS)),
            status="pending",
            message_type=_normalize_message_type(message.get("message_type"), require_reply=bool(message.get("require_reply", True))),
            cascade_depth=max(0, int(message.get("cascade_depth") or 0)),
        )
        session.add(row); session.commit(); session.refresh(row)
        return row


def fetch_cascade_parent(*, user_id: int, message_id: str) -> Optional[AIMessage]:
    """读取链路父消息（用于从 reply_to_message_id 推导 cascade_depth）。"""
    message_id = (message_id or "").strip()
    if not message_id:
        return None
    with Session(engine) as session:
        return session.exec(
            select(AIMessage).where(
                AIMessage.user_id == user_id,
                AIMessage.message_id == message_id,
            )
        ).first()


def fetch(message_id: str, user_id: int) -> Optional[AIMessage]:
    with Session(engine) as session:
        return session.exec(
            select(AIMessage).where(
                AIMessage.user_id == user_id,
                AIMessage.message_id == message_id,
            )
        ).first()



def _can_complete(row: Optional[AIMessage], replier_ai_config_id: int) -> bool:
    if row is None or int(row.to_ai_config_id) != int(replier_ai_config_id):
        return False
    requires_reply = bool(row.require_reply) or str(getattr(row, "message_type", "") or "").lower() == "inquiry"
    return requires_reply or _content_requests_response(row.content)

def complete_inbound_with_assistant_reply(
    *,
    message_id: str,
    user_id: int,
    replier_ai_config_id: int,
    content: str,
) -> Optional[Dict[str, Any]]:
    """Use the receiver's final assistant text as the reply for an AI message.

    Models sometimes answer the injected AI-to-AI message as normal assistant
    text instead of calling ``message.send+to``. This keeps the mail semantics
    reliable: a final answer in the bound receiver session still wakes the
    original sender.
    """
    message_id = (message_id or "").strip()
    content = (content or "").strip()
    if not message_id or not content:
        return None
    with Session(engine) as session:
        row = session.exec(
            select(AIMessage).where(
                AIMessage.user_id == user_id,
                AIMessage.message_id == message_id,
            )
        ).first()
        if not _can_complete(row, replier_ai_config_id):
            return None
        waited_reply = bool(row.require_reply)
        if row.status in {"replied", "failed"}:
            payload = _row_to_dict(row)
            payload["already_resolved"] = True
            return payload
        previous_status = str(row.status or "")
        row.reply_content = content
        row.status = "replied"
        row.replied_at = time.time()
        session.add(row)
        session.commit()
        session.refresh(row)
        payload = _row_to_dict(row)

    resolved_waiter = _pending_replies.resolve(message_id, payload)
    payload["waiter_resolved"] = resolved_waiter
    payload["auto_completed"] = True
    should_forward = not resolved_waiter and (not waited_reply or previous_status == "timeout")
    if should_forward:
        payload["auto_forwarded"] = not waited_reply
        _enqueue_unwaited_reply(payload)
    return payload


def _enqueue_unwaited_reply(original: Dict[str, Any]) -> None:
    """Route late/fire-and-forget replies back to the original sender.

    If the sender is still synchronously waiting, ``reply`` resolves its Future
    and this path is skipped. Otherwise the reply would only sit on the
    AIMessage row and the original AI would not get a fresh runtime interrupt.
    """
    user_id = int(original.get("user_id") or 0)
    from_ai_config_id = int(original.get("from_ai_config_id") or 0)
    to_ai_config_id = int(original.get("to_ai_config_id") or 0)
    if not all((user_id, from_ai_config_id, to_ai_config_id)):
        return

    reply_content = str(original.get("reply_content") or "").strip()
    if not reply_content:
        return

    from .ai_message_service import get_active_session_id, wake_idle_target_for_message
    target_session_id = str(original.get("from_session_id") or "").strip()
    if not target_session_id:
        target_session_id = get_active_session_id(user_id, from_ai_config_id) or f"ai_message_reply_{uuid.uuid4().hex[:14]}"

    parent_depth = int(original.get("cascade_depth") or 0)
    try:
        followup = send(
            user_id=user_id,
            from_ai_config_id=to_ai_config_id,
            to_ai_config_id=from_ai_config_id,
            content=reply_content,
            target_session_id=target_session_id,
            from_session_id=str(original.get("target_session_id") or "").strip(),
            require_reply=False,
            timeout_seconds=5,
            message_type="reply",
            cascade_depth=parent_depth + 1,
        )
    except Exception as exc:
        logger.exception(f"enqueue unwaited reply failed: {exc}")
        return

    try:
        wake_idle_target_for_message(message_id=followup.message_id, user_id=user_id)
    except Exception as exc:
        logger.exception(f"wake sender for unwaited reply failed: {exc}")


def pop_pending_for(
    user_id: int,
    ai_config_id: int,
    session_id: str,
) -> Optional[AIMessage]:
    """目标 AI worker 每轮顶部调用：取出该 (用户, AI, session) 下最早的
    pending 消息，原子地标记 delivered 并返回。

    严格按 ``target_session_id`` 匹配——这是会话隔离的关键。一个 AI
    在 session A 里跑 worker 时，绝对不会把发给它 session B 的消息抓
    走，因此不再出现 "对话对不上" 的情况。
    """
    session_id = (session_id or "").strip()
    if not session_id:
        return None
    with Session(engine) as session:
        row = session.exec(
            select(AIMessage).where(
                AIMessage.user_id == user_id,
                AIMessage.to_ai_config_id == ai_config_id,
                AIMessage.target_session_id == session_id,
                AIMessage.status == "pending",
            ).order_by(AIMessage.created_at.asc())
        ).first()
        if not row:
            return None
        row.status = "delivered"
        row.delivered_at = time.time()
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def _row_to_dict(row: AIMessage) -> Dict[str, Any]:
    return {
        "message_id": row.message_id,
        "user_id": row.user_id,
        "from_ai_config_id": row.from_ai_config_id,
        "to_ai_config_id": row.to_ai_config_id,
        "target_session_id": row.target_session_id,
        "from_session_id": row.from_session_id,
        "content": row.content,
        "status": row.status,
        "reply_content": row.reply_content,
        "require_reply": row.require_reply,
        "timeout_seconds": row.timeout_seconds,
        "message_type": getattr(row, "message_type", "notify") or "notify",
        "cascade_depth": int(getattr(row, "cascade_depth", 0) or 0),
        "delivered_at": row.delivered_at,
        "reply_reminded_at": getattr(row, "reply_reminded_at", None),
        "replied_at": row.replied_at,
        "failure_reason": row.failure_reason,
        "created_at": row.created_at,
    }


# ---------------------------------------------------------------------------
# 事件驱动的等待
# ---------------------------------------------------------------------------


