"""Task-completion notifications: build the 任务完成回执 message and start a chat
run that delivers it back to the task creator AI once its session is idle."""

import threading
import time
import uuid
from typing import Any, Dict, Optional

from sqlmodel import Session, select

from ...database import engine
from ...models import AITaskJob, AssistantAIConfig, ChatMessageCreate, ChatRun, ChatSession
from ai_runtime.inference import ai_message_service
from ..chat.chat_persistence import _save_message

TASK_COMPLETION_RECEIPT = "【任务完成回执】"
_TASK_NOTICE_WAKE_LOCK = threading.Lock()
_TASK_NOTICE_WAKE_SESSIONS: set[str] = set()


def _ai_name(session: Session, user_id: int, ai_config_id: Optional[int]) -> str:
    if not ai_config_id:
        return ""
    row = session.exec(
        select(AssistantAIConfig).where(
            AssistantAIConfig.user_id == user_id,
            AssistantAIConfig.id == int(ai_config_id),
        )
    ).first()
    return str(row.name or "").strip() if row else f"AI-{ai_config_id}"


def _completion_notice(
    *,
    job: AITaskJob,
    executor_name: str,
    summary: str,
) -> str:
    summary = str(summary or "").strip()
    lines = [
        TASK_COMPLETION_RECEIPT,
        f"- 任务ID: {job.job_id}",
        f"- 任务标题: {job.title}",
    ]
    if summary:
        lines.append(f"- 完成摘要: {summary}")
    return "\n".join(lines)


def _push_completion_to_user(
    *,
    user_id: int,
    executor_ai_config_id: int,
    executor_name: str,
    job_id: str,
    title: str,
    summary: str,
) -> Dict[str, Any]:
    """任务标记完成后，通过执行 AI 绑定的机器人把完成信息推送给用户。

    best-effort：机器人未配置或发送失败不影响任务完成与回执流程。
    不指定目标，由机器人回落到配置的默认接收人 / webhook。
    """
    try:
        from connector_runtime.bots import get as get_bot

        with Session(engine) as session:
            cfg = session.exec(
                select(AssistantAIConfig).where(
                    AssistantAIConfig.user_id == user_id,
                    AssistantAIConfig.id == executor_ai_config_id,
                )
            ).first()
        channel = str(getattr(cfg, "bot_channel", "") or "feishu").strip().lower()
        if get_bot(channel) is None:
            return {"delivered": False, "channel": channel, "reason": "channel_not_supported"}
        lines = ["【任务完成通知】"]
        if executor_name:
            lines.append(f"- 执行AI: {executor_name}")
        lines.append(f"- 任务标题: {title}")
        lines.append(f"- 任务ID: {job_id}")
        if summary:
            lines.append(f"- 完成摘要: {summary}")
        from connector_runtime.bots.messaging import Recipient, dispatcher

        delivery = dispatcher.send_text(
            user_id=user_id,
            ai_config_id=executor_ai_config_id,
            channel=channel,
            text="\n".join(lines),
            recipient=Recipient(),
        )
        return {"delivered": True, "channel": channel, "result": delivery.detail}
    except Exception as exc:
        return {"delivered": False, "error": str(exc)}


def _ai_kind_for_config(session: Session, user_id: int, ai_config_id: int) -> str:
    row = session.exec(
        select(AssistantAIConfig).where(
            AssistantAIConfig.user_id == user_id,
            AssistantAIConfig.id == int(ai_config_id),
        )
    ).first()
    if row and row.ai_role == "assistant_admin":
        return "assistant"
    return "core"


def _start_creator_notice_run(
    *,
    user_id: int,
    ai_config_id: int,
    ai_kind: str,
    session_id: str,
    session_name: str,
) -> Dict[str, Any]:
    run_id = f"run_{uuid.uuid4().hex}"
    with Session(engine) as session:
        active = ai_message_service._get_live_active_run(
            session,
            user_id,
            ai_config_id,
            session_id=session_id,
        )
        if active:
            return {"started": False, "reason": "target_active", "run_id": active.run_id}
        row = ChatRun(
            run_id=run_id,
            user_id=user_id,
            ai_config_id=ai_config_id,
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
            "ai_config_id": ai_config_id,
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
    return {"started": True, "run_id": run_id}


def _wait_idle_then_start_creator_notice_run(
    *,
    wake_key: str,
    user_id: int,
    ai_config_id: int,
    ai_kind: str,
    session_id: str,
    session_name: str,
) -> None:
    try:
        deadline = time.time() + 24 * 60 * 60
        while time.time() < deadline:
            with Session(engine) as session:
                active = ai_message_service._get_live_active_run(
                    session,
                    user_id,
                    ai_config_id,
                    session_id=session_id,
                )
            if not active:
                _start_creator_notice_run(
                    user_id=user_id,
                    ai_config_id=ai_config_id,
                    ai_kind=ai_kind,
                    session_id=session_id,
                    session_name=session_name,
                )
                return
            time.sleep(0.5)
    finally:
        with _TASK_NOTICE_WAKE_LOCK:
            _TASK_NOTICE_WAKE_SESSIONS.discard(wake_key)


def notify_task_completion(
    *,
    user_id: int,
    job_id: str,
    summary: str = "",
) -> Dict[str, Any]:
    """Return task completion to the AI/session that scheduled the job.

    The notification is idempotent per job and is appended directly to the
    scheduler AI's original session as a plain receipt message.

    自己安排给自己的任务（创建 AI == 执行 AI）不再发送回执；无论回执是否
    发送，都会通过机器人把完成信息推送给用户（best-effort）。
    """
    job_id = str(job_id or "").strip()
    summary = str(summary or "").strip()
    if not job_id:
        return {"notified": False, "reason": "missing_job_id"}

    receipt_skip_reason: Optional[str] = None
    with Session(engine) as session:
        job = session.exec(
            select(AITaskJob).where(
                AITaskJob.user_id == user_id,
                AITaskJob.job_id == job_id,
            )
        ).first()
        if not job:
            return {"notified": False, "reason": "job_not_found"}
        if job.completion_notified_at:
            return {"notified": False, "reason": "already_notified", "notified_at": job.completion_notified_at}

        # 先占住幂等位：plan.finish（计划收尾）和自然结束路径（简单任务）会进入本函数，回执与机器人推送整体只执行一次
        # （原 task.complete 调用路径已移除）
        now = time.time()
        job.completion_notified_at = now
        session.add(job)
        session.commit()
        session.refresh(job)

        executor_id = int(job.ai_config_id)
        executor_name = _ai_name(session, user_id, executor_id)
        job_title = str(job.title or "")

        creator_id = int(job.created_by_ai_config_id or 0)
        creator_session_id = str(job.created_by_session_id or "").strip()
        if not creator_id:
            receipt_skip_reason = "missing_creator_ai"
        elif not creator_session_id:
            receipt_skip_reason = "missing_creator_session"
        elif creator_id == executor_id:
            # 自己安排给自己的任务：执行会话就是创建会话，无需再给自己发回执
            receipt_skip_reason = "self_assigned"

        if receipt_skip_reason is None:
            creator_ai_kind = _ai_kind_for_config(session, user_id, creator_id)
            content = _completion_notice(job=job, executor_name=executor_name, summary=summary)

            chat_session = session.exec(
                select(ChatSession).where(
                    ChatSession.user_id == user_id,
                    ChatSession.ai_config_id == creator_id,
                    ChatSession.ai_kind == creator_ai_kind,
                    ChatSession.session_id == creator_session_id,
                ).order_by(ChatSession.updated_at.desc())
            ).first()
            session_name = str(chat_session.session_name or "").strip() if chat_session else ""
            if not session_name:
                session_name = f"任务完成回执：{job.title}"
            if chat_session is None:
                session.add(ChatSession(
                    user_id=user_id,
                    ai_config_id=creator_id,
                    ai_kind=creator_ai_kind,
                    session_id=creator_session_id,
                    session_name=session_name,
                ))

            _save_message(
                session,
                user_id,
                ChatMessageCreate(
                    role="system",
                    content=content,
                    tags=f"task_completion_notice:{job.job_id}",
                    ai_config_id=creator_id,
                    ai_kind=creator_ai_kind,
                    session_id=creator_session_id,
                    session_name=session_name,
                    total_tokens=0,
                ),
            )
            session.commit()

    # 标记完成后把完成信息通过机器人推送给用户（不持有 DB 会话，避免阻塞）
    user_push = _push_completion_to_user(
        user_id=user_id,
        executor_ai_config_id=executor_id,
        executor_name=executor_name,
        job_id=job_id,
        title=job_title,
        summary=summary,
    )

    if receipt_skip_reason is not None:
        return {"notified": False, "reason": receipt_skip_reason, "user_push": user_push}

    wake_key = f"{user_id}:{creator_id}:{creator_ai_kind}:{creator_session_id}"
    wakeup: Dict[str, Any]
    with Session(engine) as session:
        active = ai_message_service._get_live_active_run(
            session,
            user_id,
            creator_id,
            session_id=creator_session_id,
        )
    if active:
        with _TASK_NOTICE_WAKE_LOCK:
            should_wait = wake_key not in _TASK_NOTICE_WAKE_SESSIONS
            if should_wait:
                _TASK_NOTICE_WAKE_SESSIONS.add(wake_key)
        if should_wait:
            threading.Thread(
                target=_wait_idle_then_start_creator_notice_run,
                kwargs={
                    "wake_key": wake_key,
                    "user_id": user_id,
                    "ai_config_id": creator_id,
                    "ai_kind": creator_ai_kind,
                    "session_id": creator_session_id,
                    "session_name": session_name,
                },
                daemon=True,
            ).start()
        wakeup = {"started": False, "reason": "target_active", "run_id": active.run_id, "queued_after_active": True}
    else:
        wakeup = _start_creator_notice_run(
            user_id=user_id,
            ai_config_id=creator_id,
            ai_kind=creator_ai_kind,
            session_id=creator_session_id,
            session_name=session_name,
        )

    return {
        "notified": True,
        "mode": "session_message",
        "content": content,
        "to_ai_config_id": creator_id,
        "target_session_id": creator_session_id,
        "wakeup": wakeup,
        "user_push": user_push,
    }
