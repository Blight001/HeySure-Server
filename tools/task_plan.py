"""Unified ``todo.manage`` MCP for phased execution.

A plan breaks a long action into ordered phases so quality stays high. It is
*not* the same thing as a task: a task (see :mod:`tools.tasks`)
is scheduled, independent work that runs in its own session, whereas a plan can
appear inside either a normal conversation or a task conversation. The AI only
sees one tool and selects an operation with ``action``:

- ``create`` creates/replaces the phased plan
- ``get`` reads the current plan
- ``edit`` closes the current phase as completed/failed and advances
- ``delete`` abandons the current plan

Completing the final phase through ``edit`` automatically finalizes the whole
plan; there is no separate phase-complete or finish MCP.

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
        raise HTTPException(status_code=400, detail="ai_config_id is required for todo.manage")
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
            "计划已登记。现在从第 1 个阶段开始执行；完成一个阶段后调用 "
            "todo.manage(action=edit, status=completed) 更新阶段状态。系统会自动精简上一阶段上下文并下发下一阶段；"
            "最后一个阶段编辑为 completed/failed 后会自动收尾并归档。"
        ),
    }


def _plan_get(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    cfg_id = _require_ai_config_id(ai_config_id)
    session_id = _resolve_session_id(args)
    with Session(engine) as session:
        plan = plan_service.get_active_plan(session, user_id, cfg_id, session_id)
        if plan is None:
            return {"has_plan": False, "note": "当前没有进行中的计划。复杂任务请先用 knowledge.search（或 librarian.consult）检索相关知识，再用 todo.manage(action=create) 制定计划。"}
        return {"has_plan": True, "plan": plan_service.plan_progress(session, plan)}


def _phase_complete(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    cfg_id = _require_ai_config_id(ai_config_id)
    # Summary is optional: the system drives phase progression, so
    # todo.manage(action=edit) marks the current phase boundary.
    summary = str((args or {}).get("summary") or "").strip()
    status = str((args or {}).get("status") or "completed").strip().lower()
    if status not in {"completed", "failed"}:
        status = "completed"
    session_id = _resolve_session_id(args)
    with Session(engine) as session:
        plan = plan_service.get_active_plan(session, user_id, cfg_id, session_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="没有进行中的计划；请先 todo.manage(action=create) 或直接作答。")
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


def _plan_delete(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    cfg_id = _require_ai_config_id(ai_config_id)
    session_id = _resolve_session_id(args)
    reason = str((args or {}).get("reason") or (args or {}).get("summary") or "").strip()
    with Session(engine) as session:
        plan = plan_service.get_active_plan(session, user_id, cfg_id, session_id)
        if plan is None:
            return {"deleted": False, "note": "当前没有进行中的计划。"}
        progress = plan_service.abandon_plan(session, plan, reason=reason)
    return {
        "deleted": True,
        "plan": progress,
        "note": "计划已删除（历史记录保留为 abandoned）。",
    }


def _infer_todo_action(args: Dict[str, Any]) -> str:
    """Infer legacy calls that were normalized from old plan.* names."""
    action = str((args or {}).get("action") or "").strip().lower()
    if action:
        return action
    if (args or {}).get("phases") is not None or (args or {}).get("goal") or (args or {}).get("objective"):
        return "create"
    if any(key in (args or {}) for key in ("status", "summary", "outcome")):
        return "edit"
    return "get"


def _todo_manage(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    """Single MCP entry point for plan create/read/phase-close/delete."""
    action = _infer_todo_action(args)
    if action == "create":
        return _plan_create(user_id, args, ai_config_id)
    if action in {"get", "list", "read"}:
        return _plan_get(user_id, args, ai_config_id)
    if action in {"edit", "update", "complete"}:
        return _phase_complete(user_id, args, ai_config_id)
    if action in {"delete", "remove", "abandon"}:
        return _plan_delete(user_id, args, ai_config_id)
    raise HTTPException(status_code=400, detail="action 必须是 create、get、edit 或 delete。")


TODO_MANAGE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["create", "get", "edit", "delete"],
            "description": "create 创建/替换计划；get 查看计划；edit 完成当前阶段并推进；delete 删除当前计划。",
        },
        "goal": {"type": "string", "description": "create：整个任务的总体目标。"},
        "phases": {
            "type": "array",
            "description": "create：有序阶段列表，每阶段必须有 goal 与 done_signal。",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "阶段名称。"},
                    "goal": {"type": "string", "description": "阶段目标。"},
                    "done_signal": {"type": "string", "description": "阶段完成的明确判断标准。"},
                    "actions": {
                        "type": "array",
                        "description": "可选子行动。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "goal": {"type": "string"},
                                "done_signal": {"type": "string"},
                            },
                            "required": ["goal"],
                        },
                    },
                },
                "required": ["goal", "done_signal"],
            },
        },
        "status": {
            "type": "string",
            "enum": ["completed", "failed"],
            "description": "edit：当前阶段结果；默认 completed。最后阶段更新后系统自动收尾整个计划。",
        },
        "summary": {"type": "string", "description": "edit：阶段小结；delete：删除原因。"},
        "reason": {"type": "string", "description": "delete：可选删除原因。"},
        "session_id": {"type": "string", "description": "可选；默认使用当前会话。"},
    },
    "required": ["action"],
}

