"""Chat message / session / token-snapshot persistence helpers.

Pure persistence layer — no HTTP routes here. Routers and runtime
workers call these helpers to load/save chat state in a single place
so the I/O shape stays consistent.
"""

import time
from datetime import datetime
from typing import Dict, Optional, Tuple

from sqlmodel import Session, select

from ...models import ChatMessage, ChatMessageCreate, ChatSession, TokenUsageSnapshot
from .token_usage import (
    canonical_message_total,
    canonical_token_counts,
    canonical_total_sql,
)
import logging


logger = logging.getLogger(__name__)


def _save_message(
    session: Session,
    user_id: int,
    payload: ChatMessageCreate,
) -> ChatMessage:
    prompt_tokens, completion_tokens, total_tokens = canonical_token_counts(
        payload.prompt_tokens,
        payload.completion_tokens,
        payload.total_tokens,
    )
    db_msg = ChatMessage(
        user_id=user_id,
        ai_config_id=payload.ai_config_id,
        ai_kind=payload.ai_kind or "assistant",
        session_id=payload.session_id or "default",
        session_name=payload.session_name,
        role=payload.role,
        content=payload.content,
        think=payload.think,
        tags=payload.tags or "",
        model=payload.model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cache_read_tokens=payload.cache_read_tokens,
        system_prompt=payload.system_prompt,
        finish_reason=payload.finish_reason,
        latency=payload.latency,
    )
    session.add(db_msg)
    session.commit()
    session.refresh(db_msg)
    _upsert_session(session, user_id, db_msg.session_id, db_msg.session_name or "未命名会话", db_msg.ai_config_id, db_msg.ai_kind)
    _append_usage_snapshot(
        session=session,
        user_id=user_id,
        ai_config_id=db_msg.ai_config_id,
        ai_kind=db_msg.ai_kind,
        prompt_tokens=db_msg.prompt_tokens or 0,
        completion_tokens=db_msg.completion_tokens or 0,
        total_tokens=db_msg.total_tokens or 0,
    )
    try:
        from .chat_realtime import notify_history_changed

        notify_history_changed(
            user_id=user_id,
            session_id=db_msg.session_id,
            ai_config_id=db_msg.ai_config_id,
            ai_kind=db_msg.ai_kind,
            message_id=db_msg.id,
            source="persistence",
        )
    except Exception as exc:
        # The database remains the source of truth. Realtime delivery is only
        # an acceleration path and must never make a successful save fail.
        logger.exception(f"chat history notify failed message_id={db_msg.id}: {exc}")
    try:
        from connector_runtime.bots.notify import notify_saved_assistant_message

        notify_saved_assistant_message(session, db_msg)
    except Exception as exc:
        logger.exception(f"bot auto notify failed message_id={db_msg.id}: {exc}")
    return db_msg

def _rebuild_usage_snapshots(
    session: Session,
    user_id: int,
    ai_kind: str,
    ai_config_id: Optional[int] = None,
) -> None:
    snapshot_stmt = select(TokenUsageSnapshot).where(
        TokenUsageSnapshot.user_id == user_id,
        TokenUsageSnapshot.ai_kind == ai_kind,
    )
    if ai_config_id is not None:
        snapshot_stmt = snapshot_stmt.where(TokenUsageSnapshot.ai_config_id == ai_config_id)
    old_rows = session.exec(snapshot_stmt).all()
    for row in old_rows:
        session.delete(row)

    message_stmt = select(ChatMessage).where(
        ChatMessage.user_id == user_id,
        ChatMessage.ai_kind == ai_kind,
    )
    if ai_config_id is not None:
        message_stmt = message_stmt.where(ChatMessage.ai_config_id == ai_config_id)
    messages = session.exec(message_stmt).all()

    buckets: Dict[Tuple[Optional[int], str], Dict[str, int]] = {}
    for msg in messages:
        prompt, completion, total = canonical_token_counts(
            msg.prompt_tokens, msg.completion_tokens, msg.total_tokens,
        )
        if prompt <= 0 and completion <= 0 and total <= 0:
            continue
        bucket = datetime.utcfromtimestamp(msg.created_at).strftime("%Y-%m-%d")
        key = (msg.ai_config_id, bucket)
        if key not in buckets:
            buckets[key] = {"prompt": 0, "completion": 0, "total": 0}
        buckets[key]["prompt"] += prompt
        buckets[key]["completion"] += completion
        buckets[key]["total"] += total

    for (cfg_id, bucket), sums in buckets.items():
        session.add(TokenUsageSnapshot(
            user_id=user_id,
            ai_config_id=cfg_id,
            ai_kind=ai_kind,
            bucket=bucket,
            prompt_tokens=sums["prompt"],
            completion_tokens=sums["completion"],
            total_tokens=sums["total"],
            updated_at=time.time(),
        ))

    session.commit()

def _upsert_session(
    session: Session,
    user_id: int,
    session_id: str,
    session_name: str,
    ai_config_id: Optional[int],
    ai_kind: str,
) -> None:
    stmt = select(ChatSession).where(
        ChatSession.user_id == user_id,
        ChatSession.session_id == session_id,
        ChatSession.ai_kind == ai_kind,
    )
    if ai_config_id is not None:
        stmt = stmt.where(ChatSession.ai_config_id == ai_config_id)
    else:
        stmt = stmt.where(ChatSession.ai_config_id.is_(None))
    row = session.exec(stmt).first()
    if not row:
        row = ChatSession(
            user_id=user_id,
            ai_config_id=ai_config_id,
            ai_kind=ai_kind,
            session_id=session_id,
            session_name=session_name,
        )
    row.session_name = session_name or row.session_name
    row.updated_at = time.time()
    session.add(row)
    session.commit()

def _append_usage_snapshot(
    session: Session,
    user_id: int,
    ai_config_id: Optional[int],
    ai_kind: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
) -> None:
    prompt_tokens, completion_tokens, total_tokens = canonical_token_counts(
        prompt_tokens, completion_tokens, total_tokens,
    )
    if total_tokens <= 0 and prompt_tokens <= 0 and completion_tokens <= 0:
        return
    bucket = datetime.utcnow().strftime("%Y-%m-%d")
    row = session.exec(
        select(TokenUsageSnapshot).where(
            TokenUsageSnapshot.user_id == user_id,
            TokenUsageSnapshot.ai_config_id == ai_config_id,
            TokenUsageSnapshot.ai_kind == ai_kind,
            TokenUsageSnapshot.bucket == bucket,
        )
    ).first()
    if not row:
        row = TokenUsageSnapshot(
            user_id=user_id,
            ai_config_id=ai_config_id,
            ai_kind=ai_kind,
            bucket=bucket,
        )
    row.prompt_tokens += prompt_tokens
    row.completion_tokens += completion_tokens
    row.total_tokens += total_tokens
    row.updated_at = time.time()
    session.add(row)
    session.commit()
