"""AI ↔ AI 消息服务（事件驱动 + 严格 session 匹配）。

设计要点
========

* 每条 AIMessage 在入库时就绑定 ``target_session_id``——目标 AI 必须在
  匹配的 session 里才能把它 pop 出来。这样同一个 AI 在多个并行会话里
  不会串话。
* 发送方阻塞等待回复时走 ``_PendingReplyRegistry``：一个进程内的
  ``concurrent.futures.Future`` 表。对方从同一通信 session 里发回的
  ``message.send+to`` 会立即 resolve 对应 Future。
* worker 线程跑 MCP 工具时是临时 asyncio loop，跨线程用
  ``asyncio.wrap_future`` 把 ``concurrent.futures.Future`` 转成可 await
  的对象，``set_result`` 的回调会通过 ``call_soon_threadsafe`` 安全派
  发到等待方的 loop。
* 当 wait 期间整个工具调用全程都在 ``ChatRun.status='running'``，
  ``chat_scheduler`` 的 supervision_idle 计时不会触发——天然解决"AI
  等回复时被系统判定为僵死"的问题。
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import Any, Dict, Optional

from sqlmodel import Session, select

from api.database import engine
from api.models import AIMessage, AssistantAIConfig, ChatMessageCreate, ChatRun, ChatSession, User
from api.services.chat.chat_persistence import _save_message
import logging


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pending-reply registry (跨线程的事件驱动通知)
# ---------------------------------------------------------------------------


from .ai_message_state import (
    DEFAULT_REPLY_WAIT_SECONDS, _WAKE_LOCK, _content_requests_response,
    _new_message_id, _normalize_message_type, _pending_replies,
    ai_pair_channel_id, stable_peer_session_id,
)

from .ai_message_store import (
    _row_to_dict, complete_inbound_with_assistant_reply, fetch, fetch_cascade_parent,
    pop_pending_for, send,
)

def _safe_format_template(template: str, values: Dict[str, Any]) -> str:
    try:
        return str(template or "").format(**values)
    except Exception:
        return str(template or "")


def _reply_reminder_seconds(user_id: int) -> int:
    with Session(engine) as session:
        user = session.get(User, user_id)
        raw = getattr(user, "ai_message_inquiry_reminder_seconds", 3) if user else 3
    try:
        return max(0, min(3600, int(raw or 0)))
    except Exception:
        return 3


def _target_has_active_run_for_message(*, message_id: str, user_id: int) -> bool:
    with Session(engine) as session:
        row = session.exec(
            select(AIMessage).where(
                AIMessage.user_id == user_id,
                AIMessage.message_id == message_id,
            )
        ).first()
        if not row or row.status in {"replied", "failed"}:
            return False
        target_session_id = str(row.target_session_id or "").strip()
        if not target_session_id:
            return False
        return bool(
            _get_live_active_run(
                session,
                user_id,
                int(row.to_ai_config_id),
                session_id=target_session_id,
            )
        )


def _send_inquiry_reply_reminder(*, message_id: str, user_id: int, elapsed_seconds: int) -> Dict[str, Any]:
    with Session(engine) as session:
        row = session.exec(
            select(AIMessage).where(
                AIMessage.user_id == user_id,
                AIMessage.message_id == message_id,
            )
        ).first()
        if not row:
            return {"reminded": False, "reason": "message_not_found"}
        if row.status in {"replied", "failed"}:
            return {"reminded": False, "reason": f"already_{row.status}"}
        if row.reply_reminded_at:
            return {"reminded": False, "reason": "already_reminded", "reminded_at": row.reply_reminded_at}
        if not (bool(row.require_reply) or str(row.message_type or "").lower() == "inquiry"):
            return {"reminded": False, "reason": "not_reply_required"}

        from_cfg = session.exec(
            select(AssistantAIConfig).where(
                AssistantAIConfig.user_id == user_id,
                AssistantAIConfig.id == int(row.from_ai_config_id),
            )
        ).first()
        target_cfg = session.exec(
            select(AssistantAIConfig).where(
                AssistantAIConfig.user_id == user_id,
                AssistantAIConfig.id == int(row.to_ai_config_id),
            )
        ).first()
        user = session.get(User, user_id)
        if not target_cfg:
            return {"reminded": False, "reason": "target_ai_not_found"}

        from_name = str(from_cfg.name or "").strip() if from_cfg else f"AI-{row.from_ai_config_id}"
        target_name = str(target_cfg.name or "").strip() or f"AI-{row.to_ai_config_id}"
        session_id = str(row.target_session_id or "").strip()
        if not session_id:
            return {"reminded": False, "reason": "missing_target_session"}
        target_ai_config_id = int(row.to_ai_config_id)
        ai_kind = "core"
        from api.services.knowledge import kb_store

        template = kb_store.effective_system_value(
            getattr(user, "id", 0), "prompt_ai_message_inquiry_reminder",
            getattr(user, "prompt_ai_message_inquiry_reminder", ""),
        ).strip()
        content = _safe_format_template(template, {
            "message_id": row.message_id,
            "from_ai_name": from_name,
            "from_ai_config_id": row.from_ai_config_id,
            "target_ai_name": target_name,
            "target_ai_config_id": row.to_ai_config_id,
            "current_session_id": session_id,
            "content": row.content,
            "elapsed_seconds": int(elapsed_seconds or 0),
        })
        if not content.strip():
            content = (
                f"[系统提示] 消息 {row.message_id} 已等待 {int(elapsed_seconds or 0)} 秒仍未回复。"
                f"请立即调用 message.send+to 回复发送方 AI-{row.from_ai_config_id}。"
            )

        existing_chat_session = session.exec(
            select(ChatSession).where(
                ChatSession.user_id == user_id,
                ChatSession.ai_config_id == target_ai_config_id,
                ChatSession.ai_kind == ai_kind,
                ChatSession.session_id == session_id,
            ).order_by(ChatSession.updated_at.desc())
        ).first()
        session_name = str(existing_chat_session.session_name or "").strip() if existing_chat_session else f"AI通信：来自 {from_name}"
        if existing_chat_session is None:
            session.add(ChatSession(
                user_id=user_id,
                ai_config_id=target_ai_config_id,
                ai_kind=ai_kind,
                session_id=session_id,
                session_name=session_name,
            ))

        active = _get_live_active_run(session, user_id, target_ai_config_id, session_id=session_id)
        if active:
            return {"reminded": False, "reason": "target_active", "run_id": active.run_id}

        _save_message(
            session,
            user_id,
            ChatMessageCreate(
                role="user",
                content=content,
                tags=f"ai_message_reply_reminder:{row.message_id}",
                ai_config_id=target_ai_config_id,
                ai_kind=ai_kind,
                session_id=session_id,
                session_name=session_name,
                total_tokens=0,
            ),
        )
        row.reply_reminded_at = time.time()
        session.add(row)

        run_id = f"run_{uuid.uuid4().hex}"
        run = ChatRun(
            run_id=run_id,
            user_id=user_id,
            ai_config_id=target_ai_config_id,
            ai_kind=ai_kind,
            session_id=session_id,
            session_name=session_name,
            status="queued",
            stop_requested=False,
        )
        session.add(run)
        session.commit()

    from api.chat_runtime.run_state import _RUN_THREADS
    from ai_runtime.inference.core import _run_worker

    worker = threading.Thread(
        target=_run_worker,
        kwargs={
            "run_id": run_id,
            "user_id": user_id,
            "ai_config_id": target_ai_config_id,
            "ai_kind": ai_kind,
            "session_id": session_id,
            "session_name": session_name,
            "model_user_content": None,
            "merged_system_prompt": None,
            "max_steps": None,
        },
        daemon=True,
    )
    _RUN_THREADS[run_id] = worker
    worker.start()
    return {
        "reminded": True,
        "run_id": run_id,
        "target_session_id": session_id,
        "interrupted": False,
    }


async def wait_for_reply(
    *,
    message_id: str,
    user_id: int,
    timeout_seconds: int,
) -> Dict[str, Any]:
    """阻塞当前 async 上下文直到回复到达或超时。

    实现：先抢注 Future（防丢事件），再回看一遍 DB（防 register 之前
    回复就已写入），最后 ``await asyncio.wrap_future`` 等待跨线程
    set_result。
    """
    timeout = max(1, int(timeout_seconds or DEFAULT_REPLY_WAIT_SECONDS))
    fut = _pending_replies.register(message_id)
    try:
        # Race guard: 回复可能在 register 之前就完成了。
        early = fetch(message_id, user_id)
        if early and early.status in {"replied", "timeout", "failed"}:
            _pending_replies.discard(message_id)
            return _row_to_dict(early)
        reminder_after = _reply_reminder_seconds(user_id)
        should_remind = 0 < reminder_after < timeout
        wrapped = asyncio.wrap_future(fut)
        try:
            deadline = time.monotonic() + timeout
            idle_since: Optional[float] = None
            reminded = False
            while True:
                now = time.monotonic()
                remaining = deadline - now
                if remaining <= 0:
                    raise asyncio.TimeoutError()

                wait_slice = min(0.5, remaining)
                if should_remind and idle_since is not None and not reminded:
                    wait_slice = min(wait_slice, max(0.05, idle_since + reminder_after - now))

                done, _ = await asyncio.wait({wrapped}, timeout=wait_slice)
                if done:
                    return wrapped.result()

                latest = fetch(message_id, user_id)
                if latest and latest.status in {"replied", "failed"}:
                    return _row_to_dict(latest)

                target_active = _target_has_active_run_for_message(
                    message_id=message_id,
                    user_id=user_id,
                )
                if target_active:
                    idle_since = None
                    continue

                if idle_since is None:
                    idle_since = time.monotonic()
                    continue

                idle_elapsed = time.monotonic() - idle_since
                if should_remind and not reminded and idle_elapsed >= reminder_after:
                    try:
                        _send_inquiry_reply_reminder(
                            message_id=message_id,
                            user_id=user_id,
                            elapsed_seconds=reminder_after,
                        )
                    except Exception as exc:
                        logger.exception(f"inquiry reply reminder failed: {exc}")
                    reminded = True
                    idle_since = None
        except asyncio.TimeoutError:
            _pending_replies.discard(message_id)
            with Session(engine) as session:
                latest = session.exec(
                    select(AIMessage).where(AIMessage.message_id == message_id)
                ).first()
                if latest and latest.status not in {"replied", "failed"}:
                    latest.status = "timeout"
                    latest.failure_reason = "wait_for_reply timeout"
                    session.add(latest)
                    session.commit()
                    session.refresh(latest)
                return _row_to_dict(latest) if latest else {"status": "timeout"}
    finally:
        # 兜底，确保不留 dangling waiter。
        _pending_replies.discard(message_id)


# ---------------------------------------------------------------------------
# 目标 AI 的状态查询 / 唤醒
# ---------------------------------------------------------------------------



from .ai_message_routing import (
    find_corresponding_target_session_id, find_reverse_inbound_session,
    find_return_route, find_return_route_by_message_id, get_active_session_id,
    resolve_waiting_reply_from_send_message,
    resolve_waiting_reply_to_message_id_from_send_message,
)

def _get_live_active_run(
    session: Session,
    user_id: int,
    ai_config_id: int,
    *,
    session_id: str = "",
) -> Optional[ChatRun]:
    stmt = select(ChatRun).where(
        ChatRun.user_id == user_id,
        ChatRun.ai_config_id == ai_config_id,
        ChatRun.status.in_(["queued", "running"]),
    )
    if session_id:
        stmt = stmt.where(ChatRun.session_id == session_id)
    rows = session.exec(stmt.order_by(ChatRun.updated_at.desc())).all()
    now = time.time()
    for row in rows:
        if _run_thread_is_alive(str(row.run_id or "")):
            return row
        if row.status == "queued" and now - float(row.created_at or now) < 5:
            return row
        row.status = "failed"
        row.error_message = "stale active run without live worker thread"
        row.finished_at = now
        row.updated_at = now
        session.add(row)
    if rows:
        session.commit()
    return None


def _run_thread_is_alive(run_id: str) -> bool:
    if not run_id:
        return False
    try:
        from api.chat_runtime.run_state import _RUN_THREADS
        worker = _RUN_THREADS.get(run_id)
        return bool(worker and worker.is_alive())
    except Exception:
        return False


def _clear_live_run_state(run_id: str) -> None:
    if not run_id:
        return
    try:
        from api.chat_runtime.run_state import _RUN_LIVE_STATE, _RUN_STATE_LOCK
        with _RUN_STATE_LOCK:
            _RUN_LIVE_STATE.pop(run_id, None)
    except Exception:
        return


def _mark_run_interrupted(session: Session, row: ChatRun, message_id: str) -> Dict[str, Any]:
    now = time.time()
    row.stop_requested = True
    row.status = "stopped"
    row.error_message = f"interrupted by AI message {message_id}"
    row.finished_at = row.finished_at or now
    row.updated_at = now
    session.add(row)
    _clear_live_run_state(str(row.run_id or ""))
    return {
        "run_id": row.run_id,
        "session_id": row.session_id,
        "ai_kind": row.ai_kind,
        "session_name": row.session_name,
    }


def wake_idle_target_for_message(
    *,
    message_id: str,
    user_id: int,
    max_steps: Optional[int] = None,
) -> Dict[str, Any]:
    with _WAKE_LOCK:
        return _wake_idle_target_for_message_locked(
            message_id=message_id,
            user_id=user_id,
            max_steps=max_steps,
        )


def _wake_idle_target_for_message_locked(
    *,
    message_id: str,
    user_id: int,
    max_steps: Optional[int] = None,
) -> Dict[str, Any]:
    """Start a fresh target-AI conversation when an AI message would otherwise
    sit in the inbox with no worker polling it."""
    with Session(engine) as session:
        msg = session.exec(
            select(AIMessage).where(
                AIMessage.user_id == user_id,
                AIMessage.message_id == message_id,
            )
        ).first()
        if not msg:
            raise ValueError("message not found")

        target_id = int(msg.to_ai_config_id)
        target_session_id = str(msg.target_session_id or "").strip()
        if target_session_id:
            # A bound AI message must be delivered back into its intended
            # conversation. Falling back to "any active run" can steal replies
            # from Feishu/session-bound channels and make their notifier stop.
            active = _get_live_active_run(session, user_id, target_id, session_id=target_session_id)
        else:
            active = _get_live_active_run(session, user_id, target_id)
        interrupted = None
        if active:
            interrupted = _mark_run_interrupted(session, active, message_id)
            active_session_id = str(active.session_id or "").strip()
            if active_session_id:
                msg.target_session_id = active_session_id
                session.add(msg)
                target_session_id = active_session_id

        target_cfg = session.exec(
            select(AssistantAIConfig).where(
                AssistantAIConfig.user_id == user_id,
                AssistantAIConfig.id == target_id,
            )
        ).first()
        from_cfg = session.exec(
            select(AssistantAIConfig).where(
                AssistantAIConfig.user_id == user_id,
                AssistantAIConfig.id == int(msg.from_ai_config_id),
            )
        ).first()
        if not target_cfg:
            raise ValueError("target AI config not found")

        ai_kind = "core"
        from_name = str(from_cfg.name or "").strip() if from_cfg else f"AI-{msg.from_ai_config_id}"
        target_name = str(target_cfg.name or "").strip() or f"AI-{target_id}"
        # ``send()`` 已经写好了 target_session_id；这里直接复用，
        # 确保消息和 session 在同一标识下。
        session_id = target_session_id or msg.target_session_id or f"ai_message_{message_id}"
        if not msg.target_session_id:
            msg.target_session_id = session_id
            session.add(msg)
        existing_chat_session = session.exec(
            select(ChatSession).where(
                ChatSession.user_id == user_id,
                ChatSession.ai_config_id == target_id,
                ChatSession.ai_kind == ai_kind,
                ChatSession.session_id == session_id,
            ).order_by(ChatSession.updated_at.desc())
        ).first()
        interrupted_session_name = str((interrupted or {}).get("session_name") or "").strip()
        if interrupted and str(interrupted.get("session_id") or "").strip() == session_id and interrupted_session_name:
            session_name = interrupted_session_name
        elif existing_chat_session:
            session_name = str(existing_chat_session.session_name or "").strip()
        else:
            session_name = f"AI通信：来自 {from_name}"
        if existing_chat_session is None:
            chat_session = ChatSession(
                user_id=user_id,
                ai_config_id=target_id,
                ai_kind=ai_kind,
                session_id=session_id,
                session_name=session_name,
            )
            session.add(chat_session)

        run_id = f"run_{uuid.uuid4().hex}"
        row = ChatRun(
            run_id=run_id,
            user_id=user_id,
            ai_config_id=target_id,
            ai_kind=ai_kind,
            session_id=session_id,
            session_name=session_name,
            status="queued",
            stop_requested=False,
        )
        session.add(row)
        session.commit()

    from api.chat_runtime.run_state import _RUN_THREADS
    from ai_runtime.inference.core import _run_worker

    worker = threading.Thread(
        target=_run_worker,
        kwargs={
            "run_id": run_id,
            "user_id": user_id,
            "ai_config_id": target_id,
            "ai_kind": ai_kind,
            "session_id": session_id,
            "session_name": session_name,
            "model_user_content": None,
            "merged_system_prompt": None,
            "max_steps": max_steps,
        },
        daemon=True,
    )
    _RUN_THREADS[run_id] = worker
    worker.start()
    return {
        "started": True,
        "run_id": run_id,
        "session_id": session_id,
        "session_name": session_name,
        "ai_kind": ai_kind,
        "to_ai_config_id": target_id,
        "to_ai_name": target_name,
        "interrupted": bool(interrupted),
        "interrupted_run": interrupted,
    }
