"""MCP tools for the phased execution flow (the ``plan`` domain).

A plan breaks a long action into ordered phases so quality stays high. It is
*not* the same thing as a task: a task (see :mod:`tools.tasks`)
is scheduled, independent work that runs in its own session, whereas a plan can
appear inside either a normal conversation or a task conversation. ``phase`` is
a sub-operation of ``plan`` and lives under the ``plan.*`` namespace; closing a
run uses the plan-domain ``plan.finish`` (the ``plan`` namespace stays separate).

The AI drives a plan through three tools:

- ``plan.create``         commit a full multi-phase plan before acting
- ``plan.phase_complete`` finish the current phase (runtime then hides its
                          deep-thinking + MCP detail from the live context)
- ``plan.finish``         summarize the whole plan into a success/failure log

Durable plan state lives in :mod:`api.services.tasks.task_plan`; the conversation
context side effects are applied by the inference loop.
"""

import re
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from api.database import engine
from api.models import AITaskJob
from api.services.tasks import task_plan as plan_service
from connector_runtime.dispatch.device_dispatch import get_run_session_context
# get_project_root is imported inside _plan_finish to avoid circular import
# (tools.task_plan <-> mcp_runtime.mcp.registry during full MCP bootstrap)


def _run_context() -> Dict[str, Any]:
    return get_run_session_context() or {}


def _resolve_session_id(args: Dict[str, Any]) -> Optional[str]:
    explicit = str((args or {}).get("session_id") or "").strip()
    if explicit:
        return explicit
    return str(_run_context().get("session_id") or "").strip() or None


def _resolve_job_id(session: Session, user_id: int, ai_config_id: int, session_id: Optional[str]) -> Optional[str]:
    """Best-effort link of a plan to its task job.

    Task runtimes use session ids shaped like ``session_task_<job_id>[_g<n>]``;
    fall back to the newest active job for this AI when that pattern is absent.
    """
    if session_id:
        match = re.match(r"^session_task_(job_[0-9a-f]+)", str(session_id))
        if match:
            return match.group(1)
    row = session.exec(
        select(AITaskJob).where(
            AITaskJob.user_id == user_id,
            AITaskJob.ai_config_id == int(ai_config_id),
            AITaskJob.status == "running",
        ).order_by(AITaskJob.priority.desc(), AITaskJob.created_at.asc())
    ).first()
    return str(row.job_id) if row else None


def _require_ai_config_id(ai_config_id: Optional[int]) -> int:
    if not ai_config_id:
        raise HTTPException(status_code=400, detail="ai_config_id is required for plan tools")
    return int(ai_config_id)


def _plan_create(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    cfg_id = _require_ai_config_id(ai_config_id)
    goal = str((args or {}).get("goal") or (args or {}).get("objective") or "").strip()
    if not goal:
        raise HTTPException(status_code=400, detail="goal is required: 先用一句话写清整体目标。")
    try:
        phases = plan_service.normalize_phases((args or {}).get("phases"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    session_id = _resolve_session_id(args)
    with Session(engine) as session:
        job_id = _resolve_job_id(session, user_id, cfg_id, session_id)
        plan = plan_service.create_plan(
            session,
            user_id=user_id,
            ai_config_id=cfg_id,
            session_id=session_id,
            job_id=job_id,
            goal=goal,
            phases=phases,
        )
        progress = plan_service.plan_progress(session, plan)
    return {
        "created": True,
        "plan": progress,
        "next_step_hint": (
            "计划已登记。现在从第 1 个阶段开始执行；完成一个阶段后调用 plan.phase_complete 收尾该阶段"
            "（系统会自动精简上一阶段的上下文）；完成最后一个阶段时系统会自动收尾整个计划并归档，"
            "无需再单独调用收尾工具。"
        ),
    }


def _plan_get(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    cfg_id = _require_ai_config_id(ai_config_id)
    session_id = _resolve_session_id(args)
    with Session(engine) as session:
        plan = plan_service.get_active_plan(session, user_id, cfg_id, session_id)
        if plan is None:
            return {"has_plan": False, "note": "当前没有进行中的计划。复杂任务请先用 knowledge.search（或 librarian.consult）检索相关知识，再用 plan.create 制定计划。"}
        return {"has_plan": True, "plan": plan_service.plan_progress(session, plan)}


def _phase_complete(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    cfg_id = _require_ai_config_id(ai_config_id)
    # Summary is optional: the system drives phase progression, so
    # plan.phase_complete only needs to mark the boundary.
    summary = str((args or {}).get("summary") or "").strip()
    status = str((args or {}).get("status") or "completed").strip().lower()
    if status not in {"completed", "failed"}:
        status = "completed"
    session_id = _resolve_session_id(args)
    with Session(engine) as session:
        plan = plan_service.get_active_plan(session, user_id, cfg_id, session_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="没有进行中的计划；请先 plan.create 或直接作答。")
        result = plan_service.complete_current_phase(session, plan, summary=summary, status=status)
        progress = plan_service.plan_progress(session, plan)
    hint = (
        "已是最后一个阶段：系统会自动收尾整个计划并归档，你无需再调用任何收尾工具。"
        if result["all_phases_done"]
        else "本阶段已收尾、上下文已精简。系统会下发下一个阶段，按系统调度执行即可。"
    )
    return {
        "phase_completed": True,
        "finished_phase": result["finished_phase"],
        "next_phase": result["next_phase"],
        "all_phases_done": result["all_phases_done"],
        "plan": progress,
        "next_step_hint": hint,
    }


def _plan_finish(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    cfg_id = _require_ai_config_id(ai_config_id)
    summary = str((args or {}).get("summary") or "").strip()
    if not summary:
        raise HTTPException(status_code=400, detail="summary is required: 给出整个任务的完整复盘总结。")
    outcome = str((args or {}).get("outcome") or "").strip().lower()
    if outcome not in {"success", "failure"}:
        raise HTTPException(status_code=400, detail="outcome 必须是 success 或 failure。")
    session_id = _resolve_session_id(args)
    with Session(engine) as session:
        plan = plan_service.get_active_plan(session, user_id, cfg_id, session_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="没有进行中的计划可以收尾。")
        unfinished = plan_service.unfinished_phases(session, plan)
        if unfinished:
            labels = "；".join(
                f"阶段{int(item.get('seq', 0)) + 1}（{item.get('title') or item.get('goal') or '未命名'}，状态={item.get('status') or 'unknown'}）"
                for item in unfinished[:5]
            )
            more = "；..." if len(unfinished) > 5 else ""
            raise HTTPException(
                status_code=409,
                detail=(
                    "阶段性目标尚未全部收尾，已自动阻止 plan.finish。"
                    "请先继续执行当前阶段；达成结束标志后调用 plan.phase_complete，"
                    "若该阶段失败也请用 plan.phase_complete(status=failed) 如实收尾。"
                    f"未收尾阶段：{labels}{more}"
                ),
            )
        plan_service.finish_plan(session, plan, outcome=outcome, summary=summary)
        phases = plan_service.list_phases(session, plan.plan_id)
        from mcp_runtime.mcp.core import get_project_root
        workspace_root = get_project_root(user_id, cfg_id)
        try:
            log_path = plan_service.write_outcome_log(workspace_root, plan, phases, summary=summary)
        except Exception as exc:  # log failure must not abort task completion
            log_path = ""
            log_error = str(exc)
        else:
            log_error = ""
        progress = plan_service.plan_progress(session, plan)
        job_id = plan.job_id
    result: Dict[str, Any] = {
        "finished": True,
        "outcome": outcome,
        "plan_id": progress["plan_id"],
        "job_id": job_id,
        "log_path": log_path,
        "next_step_hint": (
            "任务已收尾，完整流程已写入"
            + ("成功" if outcome == "success" else "失败")
            + "日志，供后续沉淀为可复用知识。"
        ),
    }
    if log_error:
        result["log_warning"] = f"日志写入失败（不影响任务收尾）: {log_error}"

    # 计划收尾后：若有 AI 绑定了图书馆且执行者不是它，主动把本计划总结投喂给
    # 图书馆 AI，由它自行决定是否沉淀进知识库（best-effort，不影响收尾返回）。
    try:
        from api.services.knowledge.knowledge_review_trigger import trigger_plan_knowledge_review

        trigger_plan_knowledge_review(
            user_id=user_id,
            executor_ai_config_id=cfg_id,
            goal=progress.get("goal") or "",
            outcome=outcome,
            summary=summary,
            phases=progress.get("phases") or [],
            log_path=log_path,
        )
    except Exception:
        pass

    return result

