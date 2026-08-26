"""Plan-flow directives and durable automatic completion."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from sqlmodel import Session

from api.models import ChatMessageCreate
from api.services.chat.chat_persistence import _save_message
from api.services.tasks import task_plan as plan_service
from api.services.tasks.task_completion_notify import notify_task_completion
from api.chat_runtime.chat_runtime_helpers import _renew_loop_scheduled_job
from ai_runtime.inference import phase_context
from mcp_runtime.mcp import get_project_root


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlanFinalizeContext:
    session: Session
    user_id: int
    ai_config_id: int
    ai_kind: str
    session_id: str
    session_name: str
    model: str
    task_job: Any = None


def send_task_completion_notification(
    *, user_id: int, job_id: str, summary: str
) -> None:
    """Keep task completion delivery behind the plan-flow boundary."""

    notify_task_completion(user_id=user_id, job_id=job_id, summary=summary)


def append_plan_directive(
    conversation: list[dict],
    session: Session,
    plan_state: Any,
    *,
    awaiting_finish: bool,
) -> None:
    """Re-anchor the model on the complete plan and its active phase."""

    if plan_state is None:
        return
    progress = plan_service.plan_progress(session, plan_state)
    overview = phase_context.render_plan_overview(progress)
    if overview:
        conversation.append({"role": "user", "content": overview})
    if awaiting_finish:
        conversation.append(
            {
                "role": "user",
                "content": phase_context.render_finish_required_notice(plan_state.goal),
            }
        )
        return
    current = next(
        (
            phase
            for phase in progress["phases"]
            if phase["seq"] == plan_state.current_phase_seq
        ),
        None,
    )
    conversation.append(
        {
            "role": "user",
            "content": phase_context.render_phase_directive(
                current, progress["phase_count"]
            ),
        }
    )


def finalize_plan(
    context: PlanFinalizeContext,
    plan_state: Any,
    *,
    final_phase_since_ts: float,
) -> None:
    """Persist plan outcome, linked task completion, review, and user notice."""

    now_ts = time.time()
    progress = _load_progress(context.session, plan_state)
    outcome, summary = build_plan_completion_summary(progress)
    phases = progress.get("phases") or []
    _mark_final_phase_compressed(context, final_phase_since_ts, now_ts)
    log_path = _finish_plan_and_write_log(context, plan_state, outcome, summary)
    _trigger_skill_evolution(context, plan_state, progress, outcome, summary, phases, log_path)
    next_loop_job = _complete_task_job(context, summary, now_ts)
    _persist_completion_notice(context, outcome, log_path, next_loop_job)


def build_plan_completion_summary(progress: dict) -> tuple[str, str]:
    phases = progress.get("phases") or []
    outcome = (
        "failure"
        if any(str(phase.get("status")) == "failed" for phase in phases)
        else "success"
    )
    lines = ["计划各阶段已全部完成，系统自动收尾。"]
    for phase in phases:
        title = str(
            phase.get("title") or f"阶段{int(phase.get('seq', 0)) + 1}"
        )
        summary = str(phase.get("summary") or "").strip()
        lines.append(f"- {title}：{summary or '（无小结）'}")
    return outcome, "\n".join(lines)


def _load_progress(session: Session, plan_state: Any) -> dict:
    try:
        return plan_service.plan_progress(session, plan_state)
    except Exception:
        logger.exception("auto plan finalize: progress load failed")
        return {"phases": [], "goal": getattr(plan_state, "goal", "") or ""}


def _mark_final_phase_compressed(
    context: PlanFinalizeContext,
    since_ts: float,
    until_ts: float,
) -> None:
    try:
        phase_context.mark_phase_messages_compressed(
            context.session,
            user_id=context.user_id,
            ai_config_id=context.ai_config_id,
            ai_kind=context.ai_kind,
            session_id=context.session_id,
            since_ts=since_ts,
            until_ts=until_ts,
        )
    except Exception:
        logger.exception("auto plan finalize: compaction tagging failed")


def _finish_plan_and_write_log(
    context: PlanFinalizeContext,
    plan_state: Any,
    outcome: str,
    summary: str,
) -> str:
    try:
        plan_service.finish_plan(
            context.session, plan_state, outcome=outcome, summary=summary
        )
        phases = plan_service.list_phases(context.session, plan_state.plan_id)
    except Exception:
        logger.exception("auto plan finalize: finish_plan failed")
        return ""
    try:
        return plan_service.write_outcome_log(
            get_project_root(context.user_id, context.ai_config_id),
            plan_state,
            phases,
            summary=summary,
        )
    except Exception:
        logger.exception("auto plan finalize: outcome log write failed")
        return ""


def _trigger_skill_evolution(
    context: PlanFinalizeContext,
    plan_state: Any,
    progress: dict,
    outcome: str,
    summary: str,
    phases: list,
    log_path: str,
) -> None:
    try:
        from api.services.knowledge.skill_evolution import trigger_plan_skill_evolution

        trigger_plan_skill_evolution(
            user_id=context.user_id,
            executor_ai_config_id=context.ai_config_id,
            plan_id=str(getattr(plan_state, "plan_id", "") or ""),
            goal=progress.get("goal") or "",
            outcome=outcome,
            summary=summary,
            phases=phases,
            log_path=log_path,
        )
    except Exception:
        logger.exception("auto plan finalize: knowledge review trigger failed")


def _complete_task_job(
    context: PlanFinalizeContext,
    summary: str,
    now_ts: float,
) -> Any:
    task_job = context.task_job
    terminal = {"completed", "cancelled", "stopped", "error"}
    if task_job is None or str(getattr(task_job, "status", "") or "").strip() in terminal:
        return None
    try:
        try:
            send_task_completion_notification(
                user_id=context.user_id,
                job_id=str(task_job.job_id or ""),
                summary=summary,
            )
        except Exception:
            logger.exception("auto plan finalize: completion notify failed")
        next_loop_job = _renew_loop_scheduled_job(context.session, task_job, now_ts)
        if next_loop_job is None:
            task_job.status = "completed"
            task_job.finished_at = now_ts
            task_job.updated_at = now_ts
            context.session.add(task_job)
        return next_loop_job
    except Exception:
        logger.exception("auto plan finalize: task job completion failed")
        return None


def _persist_completion_notice(
    context: PlanFinalizeContext,
    outcome: str,
    log_path: str,
    next_loop_job: Any,
) -> None:
    lines = [
        "[系统提示]",
        f"所有阶段已完成，计划已自动收尾，结果：{'成功' if outcome == 'success' else '失败'}。",
    ]
    if log_path:
        label = "成功" if outcome == "success" else "失败"
        lines.append(f"- 完整流程已写入{label}日志: {log_path}")
    if next_loop_job is not None:
        lines.append(
            f"- 循环任务已续期: {next_loop_job.job_id} 回到待执行状态，等待下一轮定时触发"
        )
    try:
        _save_message(
            context.session,
            context.user_id,
            ChatMessageCreate(
                role="system",
                content="\n".join(lines),
                tags="system_notice_task_complete",
                ai_config_id=context.ai_config_id,
                ai_kind=context.ai_kind,
                session_id=context.session_id,
                session_name=context.session_name,
                model=context.model,
                total_tokens=0,
            ),
        )
        context.session.commit()
    except Exception:
        logger.exception("auto plan finalize: notice persist failed")
        context.session.rollback()
