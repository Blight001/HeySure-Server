"""Multi-phase task plan service — the single source of truth for the planned
task flow (trigger -> plan -> phased execution -> summarized end).

Both the MCP tools (``mcp_runtime``) and the inference loop (``ai_runtime``)
go through this module so plan/phase normalization, advancement and outcome
logging stay consistent across processes.

Lifecycle::

    plan.create         -> one TaskPlan (status=active) + N TaskPhase rows; phase 0 active
    plan.phase+complete -> mark current phase completed/failed, advance current_phase_seq
    plan.finish         -> mark plan completed/failed, write success/failure log file

The runtime layer is responsible for the *context* side effects (hiding the
finished phase's deep-thinking + MCP detail from the live conversation); this
module only owns the durable plan state and the human-readable logs.
"""

import json
import os
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from ...models import TaskPhase, TaskPlan

# Caps to keep a single plan tractable for the model and the context window.
MAX_PHASES = 20
MAX_ACTIONS_PER_PHASE = 20

ACTIVE_PLAN_STATUSES = {"active"}
FINISHED_PLAN_STATUSES = {"completed", "failed", "abandoned"}


def _clean_str(value: Any, limit: int = 4000) -> str:
    text = str(value or "").strip()
    return text[:limit] if len(text) > limit else text


def normalize_actions(raw: Any) -> List[Dict[str, str]]:
    """Normalize a phase's sub-action list: each action has goal + done_signal."""
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, str]] = []
    for item in raw[:MAX_ACTIONS_PER_PHASE]:
        if isinstance(item, dict):
            goal = _clean_str(item.get("goal") or item.get("title") or item.get("action"))
            done = _clean_str(item.get("done_signal") or item.get("done") or item.get("success_criteria"))
        else:
            goal = _clean_str(item)
            done = ""
        if not goal:
            continue
        out.append({"goal": goal, "done_signal": done})
    return out


def normalize_phases(raw: Any) -> List[Dict[str, Any]]:
    """Validate + normalize the phases input for ``plan.create``.

    Returns a list of ``{title, goal, done_signal, actions}``. Raises
    ``ValueError`` with a human-readable, model-facing message on bad input.
    """
    if not isinstance(raw, list) or not raw:
        raise ValueError("phases 必须是非空数组，每个阶段至少包含 goal 与 done_signal。")
    if len(raw) > MAX_PHASES:
        raise ValueError(f"阶段数量超过上限（{MAX_PHASES}）。请把计划拆得更粗，再在阶段内用子行动细化。")
    phases: List[Dict[str, Any]] = []
    for index, item in enumerate(raw):
        src = item if isinstance(item, dict) else {}
        goal = _clean_str(src.get("goal") or src.get("objective") or (item if isinstance(item, str) else ""))
        if not goal:
            raise ValueError(f"第 {index + 1} 个阶段缺少 goal（该阶段的明确目标）。")
        done = _clean_str(src.get("done_signal") or src.get("done") or src.get("success_criteria"))
        if not done:
            raise ValueError(f"第 {index + 1} 个阶段缺少 done_signal（达成该阶段的明确结束标志）。")
        title = _clean_str(src.get("title") or src.get("name"), 200) or f"阶段{index + 1}"
        phases.append({
            "title": title,
            "goal": goal,
            "done_signal": done,
            "actions": normalize_actions(src.get("actions") or src.get("sub_actions") or src.get("steps")),
        })
    return phases


def get_active_plan(
    session: Session,
    user_id: int,
    ai_config_id: int,
    session_id: Optional[str],
) -> Optional[TaskPlan]:
    """Return the active plan for this (user, ai_config, session), if any."""
    stmt = select(TaskPlan).where(
        TaskPlan.user_id == user_id,
        TaskPlan.ai_config_id == int(ai_config_id),
        TaskPlan.status.in_(list(ACTIVE_PLAN_STATUSES)),
    )
    if session_id:
        stmt = stmt.where(TaskPlan.session_id == session_id)
    return session.exec(stmt.order_by(TaskPlan.created_at.desc())).first()


def get_session_plan(
    session: Session,
    user_id: int,
    ai_config_id: int,
    session_id: Optional[str],
) -> Optional[TaskPlan]:
    """Return the most recent plan (any status) for a session — for display.

    Unlike :func:`get_active_plan` this also surfaces a finished plan so the UI
    can show the completed/failed outcome after the run ends.
    """
    if not session_id:
        return None
    return session.exec(
        select(TaskPlan).where(
            TaskPlan.user_id == user_id,
            TaskPlan.ai_config_id == int(ai_config_id),
            TaskPlan.session_id == session_id,
        ).order_by(TaskPlan.created_at.desc())
    ).first()


def progress_stage(session: Session, plan: Optional[TaskPlan], *, is_task_session: bool) -> str:
    """Derive the coarse UI stage: planning / executing / finishing / finished / none."""
    if plan is None:
        return "planning" if is_task_session else "none"
    if plan.status in FINISHED_PLAN_STATUSES:
        return "finished"
    return "finishing" if awaiting_finish(session, plan) else "executing"


def list_phases(session: Session, plan_id: str) -> List[TaskPhase]:
    return session.exec(
        select(TaskPhase).where(TaskPhase.plan_id == plan_id).order_by(TaskPhase.seq.asc())
    ).all()


def current_phase(session: Session, plan: TaskPlan) -> Optional[TaskPhase]:
    return session.exec(
        select(TaskPhase).where(
            TaskPhase.plan_id == plan.plan_id,
            TaskPhase.seq == plan.current_phase_seq,
        )
    ).first()


def awaiting_finish(session: Session, plan: Optional[TaskPlan]) -> bool:
    """True when every phase is done and only ``plan.finish`` remains.

    After the last phase's ``plan.phase+complete`` the plan's ``current_phase_seq``
    stays on the final phase (no next phase to advance to); that phase being
    finished is the signal the whole plan should be summarized and closed.
    """
    if plan is None or plan.status not in ACTIVE_PLAN_STATUSES:
        return False
    phase = current_phase(session, plan)
    return bool(
        phase is not None
        and phase.status in {"completed", "failed"}
        and phase.seq >= plan.phase_count - 1
    )


def unfinished_phases(session: Session, plan: Optional[TaskPlan]) -> List[Dict[str, Any]]:
    """Return phases that have not been explicitly closed yet.

    A phase is considered closed once ``plan.phase+complete`` records either
    ``completed`` or ``failed``. The final plan outcome can still be success or
    failure, but ``plan.finish`` must not skip unclosed phase boundaries.
    """
    if plan is None or plan.status not in ACTIVE_PLAN_STATUSES:
        return []
    out: List[Dict[str, Any]] = []
    for phase in list_phases(session, plan.plan_id):
        if phase.status in {"completed", "failed"}:
            continue
        out.append(_phase_dict(phase))
    return out


def get_active_plan_for_job(
    session: Session,
    user_id: int,
    ai_config_id: int,
    *,
    job_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Optional[TaskPlan]:
    """Find an active plan linked to a task job or its session."""
    if session_id:
        plan = get_active_plan(session, user_id, ai_config_id, session_id)
        if plan is not None:
            return plan
    if job_id:
        return session.exec(
            select(TaskPlan).where(
                TaskPlan.user_id == user_id,
                TaskPlan.ai_config_id == int(ai_config_id),
                TaskPlan.job_id == str(job_id),
                TaskPlan.status.in_(list(ACTIVE_PLAN_STATUSES)),
            ).order_by(TaskPlan.created_at.desc())
        ).first()
    return None


def _phase_dict(phase: TaskPhase) -> Dict[str, Any]:
    try:
        actions = json.loads(phase.actions_json) if phase.actions_json else []
    except Exception:
        actions = []
    return {
        "seq": phase.seq,
        "title": phase.title,
        "goal": phase.goal,
        "done_signal": phase.done_signal,
        "actions": actions if isinstance(actions, list) else [],
        "status": phase.status,
        "summary": phase.summary or "",
    }


def plan_progress(session: Session, plan: TaskPlan) -> Dict[str, Any]:
    """A compact, model-facing snapshot of the plan and its phases."""
    phases = [_phase_dict(p) for p in list_phases(session, plan.plan_id)]
    return {
        "plan_id": plan.plan_id,
        "goal": plan.goal,
        "status": plan.status,
        "outcome": plan.outcome or "",
        "phase_count": plan.phase_count,
        "current_phase_seq": plan.current_phase_seq,
        "phases": phases,
    }


def create_plan(
    session: Session,
    *,
    user_id: int,
    ai_config_id: int,
    session_id: Optional[str],
    job_id: Optional[str],
    goal: str,
    phases: List[Dict[str, Any]],
    replace_existing: bool = True,
) -> TaskPlan:
    """Create a plan + its phases. Abandons any prior active plan in this scope."""
    if replace_existing:
        for stale in session.exec(
            select(TaskPlan).where(
                TaskPlan.user_id == user_id,
                TaskPlan.ai_config_id == int(ai_config_id),
                TaskPlan.session_id == session_id,
                TaskPlan.status.in_(list(ACTIVE_PLAN_STATUSES)),
            )
        ).all():
            stale.status = "abandoned"
            stale.updated_at = time.time()
            stale.finished_at = time.time()
            session.add(stale)

    now = time.time()
    plan = TaskPlan(
        plan_id=f"plan_{uuid.uuid4().hex[:12]}",
        user_id=user_id,
        ai_config_id=int(ai_config_id),
        job_id=job_id or None,
        session_id=session_id or None,
        goal=_clean_str(goal, 8000),
        status="active",
        phase_count=len(phases),
        current_phase_seq=0,
        created_at=now,
        updated_at=now,
    )
    session.add(plan)
    for seq, phase in enumerate(phases):
        session.add(TaskPhase(
            phase_id=f"phase_{uuid.uuid4().hex[:12]}",
            plan_id=plan.plan_id,
            user_id=user_id,
            seq=seq,
            title=phase["title"],
            goal=phase["goal"],
            done_signal=phase["done_signal"],
            actions_json=json.dumps(phase.get("actions") or [], ensure_ascii=False),
            status="active" if seq == 0 else "pending",
            started_at=now if seq == 0 else None,
        ))
    session.commit()
    session.refresh(plan)
    return plan


def complete_current_phase(
    session: Session,
    plan: TaskPlan,
    *,
    summary: str,
    status: str = "completed",
) -> Dict[str, Any]:
    """Mark the in-progress phase done, advance to the next one.

    Returns ``{finished_phase, next_phase, all_phases_done}``.
    """
    if status not in {"completed", "failed"}:
        status = "completed"
    now = time.time()
    finished = current_phase(session, plan)
    finished_dict: Optional[Dict[str, Any]] = None
    if finished is not None:
        finished.status = status
        finished.summary = _clean_str(summary, 8000)
        finished.finished_at = now
        session.add(finished)
        finished_dict = _phase_dict(finished)

    next_seq = plan.current_phase_seq + 1
    nxt = session.exec(
        select(TaskPhase).where(TaskPhase.plan_id == plan.plan_id, TaskPhase.seq == next_seq)
    ).first()
    all_done = nxt is None
    if nxt is not None:
        nxt.status = "active"
        if nxt.started_at is None:
            nxt.started_at = now
        session.add(nxt)
        plan.current_phase_seq = next_seq
    plan.updated_at = now
    session.add(plan)
    session.commit()
    return {
        "finished_phase": finished_dict,
        "next_phase": _phase_dict(nxt) if nxt is not None else None,
        "all_phases_done": all_done,
    }


def finish_plan(
    session: Session,
    plan: TaskPlan,
    *,
    outcome: str,
    summary: str,
) -> Dict[str, Any]:
    """Finalize the whole plan as success or failure."""
    outcome = "success" if str(outcome or "").strip().lower() in {"success", "succeed", "ok", "成功"} else "failure"
    now = time.time()
    plan.status = "completed" if outcome == "success" else "failed"
    plan.outcome = outcome
    plan.summary = _clean_str(summary, 20000)
    plan.updated_at = now
    plan.finished_at = now
    session.add(plan)
    # Leave a sane status on any still-open phase so logs read coherently.
    for phase in list_phases(session, plan.plan_id):
        if phase.status in {"pending", "active"}:
            phase.status = "completed" if outcome == "success" else "failed"
            if phase.finished_at is None:
                phase.finished_at = now
            session.add(phase)
    session.commit()
    return plan_progress(session, plan)


# --------------------------------------------------------------------------- #
# Outcome logs — durable, reusable knowledge in the AI workspace.
# --------------------------------------------------------------------------- #

def _outcome_dir(workspace_root: str, outcome: str) -> str:
    sub = "success" if outcome == "success" else "failure"
    return os.path.join(workspace_root, "logs", sub)


def render_outcome_log(plan: TaskPlan, phases: List[TaskPhase], *, summary: str) -> str:
    """Render a self-contained Markdown record of one full task run."""
    outcome = plan.outcome or ("success" if plan.status == "completed" else "failure")
    badge = "✅ 成功" if outcome == "success" else "❌ 失败"
    finished_at = plan.finished_at or time.time()
    lines: List[str] = [
        f"# {badge} · {plan.goal or '(无目标描述)'}",
        "",
        f"- 计划ID: {plan.plan_id}",
        f"- 任务Job: {plan.job_id or '-'}",
        f"- 结束时间: {datetime.fromtimestamp(finished_at).isoformat(sep=' ', timespec='seconds')}",
        f"- 阶段数: {plan.phase_count}",
        "",
        "## 总结",
        "",
        _clean_str(summary, 20000) or "(无)",
        "",
        "## 各阶段流程",
        "",
    ]
    for phase in phases:
        mark = {"completed": "✅", "failed": "❌"}.get(phase.status, "•")
        lines.append(f"### {mark} 阶段{phase.seq + 1}：{phase.title}")
        lines.append(f"- 目标: {phase.goal}")
        lines.append(f"- 结束标志: {phase.done_signal}")
        try:
            actions = json.loads(phase.actions_json) if phase.actions_json else []
        except Exception:
            actions = []
        if isinstance(actions, list) and actions:
            lines.append("- 子行动:")
            for action in actions:
                if isinstance(action, dict):
                    goal = str(action.get("goal") or "").strip()
                    done = str(action.get("done_signal") or "").strip()
                    lines.append(f"  - {goal}" + (f"（结束标志：{done}）" if done else ""))
        if phase.summary:
            lines.append(f"- 小结: {phase.summary}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_outcome_log(
    workspace_root: str,
    plan: TaskPlan,
    phases: List[TaskPhase],
    *,
    summary: str,
) -> str:
    """Write the run's success/failure log into the AI workspace; return its path."""
    outcome = plan.outcome or ("success" if plan.status == "completed" else "failure")
    target_dir = _outcome_dir(workspace_root, outcome)
    os.makedirs(target_dir, exist_ok=True)
    stamp = datetime.fromtimestamp(plan.finished_at or time.time()).strftime("%Y%m%d_%H%M%S")
    filename = f"{stamp}_{plan.plan_id}.md"
    path = os.path.join(target_dir, filename)
    body = render_outcome_log(plan, phases, summary=summary)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    return path
