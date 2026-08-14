"""Durably enqueue an AI turn for each pending AI-review workflow step."""

from __future__ import annotations

import json
import time
import uuid
from typing import Optional

from sqlmodel import Session, select

from api.database import engine
from api.models import (
    AssistantAIConfig,
    ChatMessage,
    ChatRun,
    ChatSession,
    WorkflowConfirmation,
    WorkflowRun,
)

from .ai_interaction import AI_REVIEW_TYPES, ai_review_payload


def _ai_kind(config: AssistantAIConfig) -> str:
    return "assistant" if config.ai_role == "assistant_admin" else "core"


def _notice_content(session: Session, run: WorkflowRun, item: WorkflowConfirmation) -> str:
    review = ai_review_payload(session, run, item)
    lines = [
        "【自动化卡片交互请求】",
        f"- 运行ID: {run.id}",
        f"- 步骤ID: {item.step_id}",
        f"- 当前 AI 节点任务: {item.risk_summary}",
        "- 此前完整运行过程:",
        json.dumps(review["execution_trace"], ensure_ascii=False, default=str),
    ]
    lines.extend([
        "请核对说明与当前上下文；确认后调用 automation.manage，action=respond、approved=true。",
        "如流程需要补充参数，请将参数对象放入 parameters；拒绝时 approved=false 并说明原因。",
    ])
    lines.append("回调必须携带本消息中的 run_id。")
    return "\n".join(lines)


def _origin_session(
    session: Session,
    item: WorkflowConfirmation,
    run: WorkflowRun,
) -> Optional[ChatSession]:
    try:
        variables = json.loads(run.variables_json or "{}")
    except Exception:
        return None
    origin = variables.get("_chat_origin") if isinstance(variables, dict) else None
    origin_run_id = str(origin.get("run_id") or "").strip() if isinstance(origin, dict) else ""
    origin_session_id = str(origin.get("session_id") or "").strip() if isinstance(origin, dict) else ""
    if not origin_run_id or not origin_session_id:
        return None
    origin_run = session.exec(select(ChatRun).where(
        ChatRun.run_id == origin_run_id,
        ChatRun.user_id == item.requested_user_id,
        ChatRun.ai_config_id == item.ai_config_id,
        ChatRun.session_id == origin_session_id,
    )).first()
    if not origin_run:
        return None
    return session.exec(select(ChatSession).where(
        ChatSession.user_id == item.requested_user_id,
        ChatSession.ai_config_id == item.ai_config_id,
        ChatSession.ai_kind == origin_run.ai_kind,
        ChatSession.session_id == origin_session_id,
    )).first()


def _ensure_session(
    session: Session,
    item: WorkflowConfirmation,
    config: AssistantAIConfig,
    run: WorkflowRun,
) -> ChatSession:
    origin = _origin_session(session, item, run)
    if origin:
        return origin
    session_id = f"workflow_interaction_{item.run_id}"
    kind = _ai_kind(config)
    row = session.exec(select(ChatSession).where(
        ChatSession.user_id == item.requested_user_id,
        ChatSession.ai_config_id == item.ai_config_id,
        ChatSession.ai_kind == kind,
        ChatSession.session_id == session_id,
    )).first()
    if row:
        return row
    row = ChatSession(
        user_id=item.requested_user_id,
        ai_config_id=item.ai_config_id,
        ai_kind=kind,
        session_id=session_id,
        session_name=f"自动化 AI 审核：{item.run_id}",
    )
    session.add(row)
    session.flush()
    return row


def _active_chat_run(session: Session, item: WorkflowConfirmation, chat: ChatSession) -> Optional[ChatRun]:
    return session.exec(select(ChatRun).where(
        ChatRun.user_id == item.requested_user_id,
        ChatRun.ai_config_id == item.ai_config_id,
        ChatRun.session_id == chat.session_id,
        ChatRun.status.in_(["queued", "running"]),
    ).order_by(ChatRun.created_at.desc())).first()


def _enqueue_notice(session: Session, item: WorkflowConfirmation) -> bool:
    config = session.exec(select(AssistantAIConfig).where(
        AssistantAIConfig.user_id == item.requested_user_id,
        AssistantAIConfig.id == item.ai_config_id,
    )).first()
    run = session.get(WorkflowRun, item.run_id)
    if not config or not run:
        return False
    chat = _ensure_session(session, item, config, run)
    content = _notice_content(session, run, item)
    if item.notified_at is None:
        session.add(ChatMessage(
            user_id=item.requested_user_id,
            ai_config_id=item.ai_config_id,
            ai_kind=chat.ai_kind,
            session_id=chat.session_id,
            session_name=chat.session_name,
            role="system",
            content=content,
            tags=f"workflow_interaction:{item.id}",
            total_tokens=0,
        ))
        item.notified_at = time.time()
        session.add(item)
        session.flush()
    if _active_chat_run(session, item, chat):
        return False
    notification_id = f"run_{uuid.uuid4().hex}"
    session.add(ChatRun(
        run_id=notification_id,
        user_id=item.requested_user_id,
        ai_config_id=item.ai_config_id,
        ai_kind=chat.ai_kind,
        session_id=chat.session_id,
        session_name=chat.session_name,
        status="queued",
        stop_requested=False,
        worker_kwargs_json=json.dumps({"model_user_content": content}, ensure_ascii=False),
    ))
    item.notification_run_id = notification_id
    session.add(item)
    return True


def process_pending_ai_interactions(limit: int = 50) -> int:
    """Create one durable AI turn per pending AI review, retrying while its session is busy."""
    now = time.time()
    with Session(engine) as session:
        ids = session.exec(select(WorkflowConfirmation.id).where(
            WorkflowConfirmation.status == "pending",
            WorkflowConfirmation.confirmation_type.in_(AI_REVIEW_TYPES),
            WorkflowConfirmation.ai_config_id.is_not(None),
            WorkflowConfirmation.notification_run_id == "",
            WorkflowConfirmation.expires_at > now,
        ).order_by(WorkflowConfirmation.created_at).limit(limit)).all()
    count = 0
    for item_id in ids:
        with Session(engine) as session:
            item = session.exec(select(WorkflowConfirmation).where(
                WorkflowConfirmation.id == item_id,
                WorkflowConfirmation.status == "pending",
                WorkflowConfirmation.notification_run_id == "",
            ).with_for_update(skip_locked=True)).first()
            if item and _enqueue_notice(session, item):
                count += 1
            if item:
                session.commit()
    return count
