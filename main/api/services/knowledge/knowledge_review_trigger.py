"""Plan-completion → knowledge review trigger.

当一个**非图书馆**的 AI 完成一个计划（``plan.finish``）时，主动把这份计划的
总结投喂给"绑定了图书馆（知识工坊）的那个 AI"，并唤醒它一次，由它**自己决定**
是否把其中可复用的经验/教训沉淀进知识库（``knowledge.manage action=record_experience``，
直接 active、无需用户审批）。

设计要点：
- 复用 ``task_completion_notify`` 已验证的"等目标会话空闲→唤醒一次 run"机制；
- 投喂落在图书馆 AI 的一个**专用会话**（``kb_auto_review``），不污染它与用户的对话；
- 防回环：图书馆 AI 自己跑的计划不触发（executor == 绑定 AI 时跳过）；
- best-effort：任何失败都不影响 ``plan.finish`` 本身。
"""

import threading
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from ..database import engine
from ..models import AssistantAIConfig, ChatMessageCreate, ChatSession
from ..services.chat_persistence import _save_message
from ..services.task_completion_notify import (
    _TASK_NOTICE_WAKE_LOCK,
    _TASK_NOTICE_WAKE_SESSIONS,
    _ai_kind_for_config,
    _ai_name,
    _start_creator_notice_run,
    _wait_idle_then_start_creator_notice_run,
)
from ..workshop_bindings import bound_config_id_for_agent

# 图书馆 AI 接收"计划完成→请审核沉淀"简报的专用会话（与用户对话隔离）。
REVIEW_SESSION_ID = "kb_auto_review"
REVIEW_SESSION_NAME = "知识库自动审核"
KNOWLEDGE_REVIEW_TAG = "kb_review_request"


def _render_briefing(
    *,
    executor_name: str,
    goal: str,
    outcome: str,
    summary: str,
    phases: List[Dict[str, Any]],
    log_path: str,
) -> str:
    badge = "成功" if outcome == "success" else "失败"
    lines = [
        "【计划完成 · 待你审核是否沉淀】",
        f"- 执行AI: {executor_name or '(未知)'}",
        f"- 计划目标: {goal or '(无)'}",
        f"- 结果: {badge}",
    ]
    summary = str(summary or "").strip()
    if summary:
        lines.append(f"- 完成总结: {summary}")
    if phases:
        lines.append("- 各阶段小结:")
        for phase in phases:
            seq = int(phase.get("seq", 0)) + 1
            title = str(phase.get("title") or phase.get("goal") or "").strip()
            status = str(phase.get("status") or "").strip()
            phase_summary = str(phase.get("summary") or "").strip()
            head = f"  {seq}. [{status}] {title}".rstrip()
            lines.append(head + (f"：{phase_summary}" if phase_summary else ""))
    if log_path:
        lines.append(f"- 完整复盘日志: {log_path}")
    lines.append("")
    lines.append(
        "你绑定了图书馆。请判断这次计划是否产生了**值得长期保存、可复用**的经验或教训："
        "\n- 若有：先用 knowledge.search 检索是否已有同类条目，避免重复；确无重复且确有价值，"
        "再用 knowledge.manage(action=record_experience) 直接写入知识库（status 即 active，无需用户审批），"
        "triggers 用与该场景相关的关键词，方便日后派任务前自动命中。"
        "\n- 若价值不高、过于具体无法泛化、或已有同类条目：无需任何操作，本次审核可直接结束。"
    )
    return "\n".join(lines)


def _do_trigger(
    *,
    user_id: int,
    executor_ai_config_id: int,
    goal: str,
    outcome: str,
    summary: str,
    phases: List[Dict[str, Any]],
    log_path: str,
) -> None:
    try:
        from library.engine import device_id_for_user
    except Exception:
        return
    device_id = device_id_for_user(user_id)
    librarian_id = bound_config_id_for_agent(user_id, device_id)
    if not librarian_id:
        return  # 没有 AI 绑定图书馆，跳过
    if int(librarian_id) == int(executor_ai_config_id):
        return  # 图书馆 AI 自己跑的计划，不自投自审，防回环

    with Session(engine) as session:
        librarian_cfg = session.exec(
            select(AssistantAIConfig).where(
                AssistantAIConfig.user_id == user_id,
                AssistantAIConfig.id == int(librarian_id),
            )
        ).first()
        if librarian_cfg is None or not bool(librarian_cfg.enabled):
            return
        ai_kind = _ai_kind_for_config(session, user_id, int(librarian_id))
        executor_name = _ai_name(session, user_id, int(executor_ai_config_id))
        content = _render_briefing(
            executor_name=executor_name,
            goal=goal,
            outcome=outcome,
            summary=summary,
            phases=phases,
            log_path=log_path,
        )

        chat_session = session.exec(
            select(ChatSession).where(
                ChatSession.user_id == user_id,
                ChatSession.ai_config_id == int(librarian_id),
                ChatSession.ai_kind == ai_kind,
                ChatSession.session_id == REVIEW_SESSION_ID,
            )
        ).first()
        if chat_session is None:
            session.add(ChatSession(
                user_id=user_id,
                ai_config_id=int(librarian_id),
                ai_kind=ai_kind,
                session_id=REVIEW_SESSION_ID,
                session_name=REVIEW_SESSION_NAME,
            ))
        _save_message(
            session,
            user_id,
            ChatMessageCreate(
                role="system",
                content=content,
                tags=KNOWLEDGE_REVIEW_TAG,
                ai_config_id=int(librarian_id),
                ai_kind=ai_kind,
                session_id=REVIEW_SESSION_ID,
                session_name=REVIEW_SESSION_NAME,
                total_tokens=0,
            ),
        )
        session.commit()

    # 唤醒图书馆 AI 处理这条审核简报：复用任务回执那套"等空闲→拉起一次 run"。
    from ai_runtime.inference import ai_message_service

    wake_key = f"{user_id}:{librarian_id}:{ai_kind}:{REVIEW_SESSION_ID}"
    with Session(engine) as session:
        active = ai_message_service._get_live_active_run(
            session, user_id, int(librarian_id), session_id=REVIEW_SESSION_ID
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
                    "ai_config_id": int(librarian_id),
                    "ai_kind": ai_kind,
                    "session_id": REVIEW_SESSION_ID,
                    "session_name": REVIEW_SESSION_NAME,
                },
                daemon=True,
            ).start()
    else:
        _start_creator_notice_run(
            user_id=user_id,
            ai_config_id=int(librarian_id),
            ai_kind=ai_kind,
            session_id=REVIEW_SESSION_ID,
            session_name=REVIEW_SESSION_NAME,
        )


def trigger_plan_knowledge_review(
    *,
    user_id: int,
    executor_ai_config_id: int,
    goal: str,
    outcome: str,
    summary: str,
    phases: Optional[List[Dict[str, Any]]] = None,
    log_path: str = "",
) -> None:
    """在 ``plan.finish`` 后调用（best-effort，不阻塞）。

    若该用户有 AI 绑定了图书馆、且本计划的执行者不是该图书馆 AI，则把计划总结
    投喂给图书馆 AI 并唤醒它，由它自行决定是否沉淀进知识库。
    """
    phases_list = list(phases or [])

    def _runner() -> None:
        try:
            _do_trigger(
                user_id=int(user_id),
                executor_ai_config_id=int(executor_ai_config_id),
                goal=str(goal or ""),
                outcome=str(outcome or ""),
                summary=str(summary or ""),
                phases=phases_list,
                log_path=str(log_path or ""),
            )
        except Exception:
            # 自动审核是增益能力，任何失败都不应影响计划收尾。
            pass

    threading.Thread(target=_runner, daemon=True).start()
