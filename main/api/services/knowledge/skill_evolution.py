"""Automatic, immediately-active Skill evolution from completed AI plans."""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Dict, List, Optional

def _compact(text: Any, limit: int = 500) -> str:
    value = re.sub(r"\s+", " ", str(text or "").strip())
    return value[:limit] + "…" if len(value) > limit else value


def _plan_skill_body(goal: str, outcome: str, summary: str, phases: List[Dict[str, Any]]) -> str:
    lines = [
        "## 适用场景",
        "",
        _compact(goal, 800),
        "",
        "## 执行步骤",
        "",
    ]
    for index, phase in enumerate(phases, 1):
        title = _compact(phase.get("title") or phase.get("goal") or f"阶段 {index}", 240)
        phase_summary = _compact(phase.get("summary") or "", 700)
        lines.append(f"{index}. {title}" + (f"：{phase_summary}" if phase_summary else ""))
    lines.extend([
        "",
        "## 成功判定",
        "",
        f"本次结果：{outcome}。",
        _compact(summary, 1200),
        "",
        "## 自动进化说明",
        "",
        "本 Skill 由 AI 完成计划后自动提炼，可在后续同类任务中通过 @Skill 显式调用。",
    ])
    return "\n".join(lines).strip() + "\n"


def _normalized_plan(goal: str, outcome: str, phases: Optional[List[Dict[str, Any]]]):
    normalized_goal = _compact(goal, 800)
    phase_rows = [item for item in (phases or []) if isinstance(item, dict)]
    if str(outcome or "").strip().lower() != "success" or not normalized_goal:
        return None, "not_successful_or_missing_goal"
    if not phase_rows:
        return None, "no_plan_phases"
    return (normalized_goal, phase_rows), ""


def _skill_identity(user_id: int, ai_config_id: int, goal: str) -> str:
    key = hashlib.sha256(
        f"{int(user_id)}:{int(ai_config_id)}:{goal.casefold()}".encode("utf-8")
    ).hexdigest()[:16]
    return f"auto/{int(ai_config_id)}/{key}"


def _previous_version(existing, thought_meta_to_row) -> int:
    if existing is None:
        return 0
    row = thought_meta_to_row(existing[2], existing[0])
    return int(float(row.get("version") or 0))


def _evolved_skill_row(
    *, slug: str, goal: str, summary: str, plan_id: Optional[str], ai_config_id: int,
    version: int, existing, now: float,
) -> Dict[str, Any]:
    return {
        "slug": slug,
        "displayName": goal[:120],
        "summary": _compact(summary or goal, 240),
        "triggers": [],
        "version": str(version + 1),
        "source": "auto:plan",
        "installed_at": float(existing[2].get("installed_at") or now) if existing else now,
        "auto_enabled": True,
        "endpoint_kind": "any",
        "scope": "ai",
        "scope_target": str(int(ai_config_id)),
        "owner_ai_config_id": int(ai_config_id),
        "source_plan_id": str(plan_id or ""),
        "evolved_at": now,
        "trust": {"verdict": "self-authored-auto"},
    }


def evolve_skill_from_plan(
    *,
    user_id: int,
    executor_ai_config_id: int,
    plan_id: Optional[str],
    goal: str,
    outcome: str,
    summary: str,
    phases: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Create or update an active AI-owned Skill; never blocks task completion."""
    from .librarian_core import _find_thought, _normalize_triggers, _thought_meta_to_row, _upsert_thought

    plan, reason = _normalized_plan(goal, outcome, phases)
    if plan is None:
        return {"evolved": False, "reason": reason}
    normalized_goal, phase_rows = plan
    slug = _skill_identity(user_id, executor_ai_config_id, normalized_goal)
    existing = _find_thought(int(user_id), slug)
    previous_version = _previous_version(existing, _thought_meta_to_row)
    phase_titles = [
        _compact(item.get("title") or item.get("goal") or "", 80)
        for item in phase_rows
    ]
    triggers = _normalize_triggers([normalized_goal, *phase_titles])
    now = time.time()
    row = _evolved_skill_row(
        slug=slug,
        goal=normalized_goal,
        summary=summary,
        plan_id=plan_id,
        ai_config_id=executor_ai_config_id,
        version=previous_version,
        existing=existing,
        now=now,
    )
    row["triggers"] = triggers
    body = _plan_skill_body(normalized_goal, str(outcome or "success"), summary, phase_rows)
    merged = _upsert_thought(int(user_id), row, body=body)
    return {
        "evolved": True,
        "created": existing is None,
        "updated": existing is not None,
        "skill": merged,
    }


def trigger_plan_skill_evolution(**kwargs: Any) -> None:
    """Fire-and-forget wrapper used by plan finalization."""
    try:
        evolve_skill_from_plan(**kwargs)
    except Exception:
        # Skill evolution is an enhancement and must never fail the plan.
        return
