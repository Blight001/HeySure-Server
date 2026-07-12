"""Task-system helpers: normalize the per-config ``system_auto_control`` blob and
its task items, decode task payloads, and resolve task session ids."""

import json
from typing import Any, Dict, Optional

from sqlmodel import Session, select

from ...models import (
    DEFAULT_COMPRESSION_PROMPT,
    DEFAULT_RESUME_TASK_PROMPT,
    DEFAULT_START_TASK_PROMPT,
    DEFAULT_SUPERVISION_PROMPT,
    DEFAULT_TASK_PLAN_FLOW_PROMPT,
    AITaskJob,
    ChatRun,
)

DEFAULT_SYSTEM_AUTO_CONTROL: Dict[str, Any] = {
    "enabled": True,
    "start_task_prompt": DEFAULT_START_TASK_PROMPT,
    "resume_task_prompt": DEFAULT_RESUME_TASK_PROMPT,
    "supervision_prompt": DEFAULT_SUPERVISION_PROMPT,
    "compression_prompt": DEFAULT_COMPRESSION_PROMPT,
    "tasks": [],
}
TASK_FLOW_PROMPT_KEYS = (
    "start_task_prompt",
    "resume_task_prompt",
    "supervision_prompt",
    "compression_prompt",
)

TASK_RUNTIME_REQUIRED_TOOLS = {
    # ``task.list`` was folded into the unified ``task.manage`` tool; the runtime
    # needs it for action=list; every action is open to a library-bound AI.
    "task.manage",
    "message.send+to+ai",
    # Planned task flow: create/get/edit/delete all use one MCP. Editing the
    # final phase automatically closes the plan.
    # Small/non-plan tasks no longer require an explicit completion MCP call.
    "todo.manage",
}

# Injected into the task-runtime system prompt. The flow is enforced by the
# runtime (see ai_runtime.inference.core): todo.create first, the system hands over
# each phase, and todo.edit advances/automatically closes the final phase.
# Simple tasks do not require an explicit completion tool. The text is editable as a
# 固有思想 system prompt (key ``task_plan_flow_prompt``); this constant is only
# the built-in fallback.
TASK_PLAN_FLOW_PROMPT = DEFAULT_TASK_PLAN_FLOW_PROMPT


def with_workspace_read_by_name_compat(tools: set[str]) -> set[str]:
    return {
        str(item).strip()
        for item in (tools or set())
        if str(item).strip()
        and (
            not str(item).strip().startswith("workspace.")
            or str(item).strip() in {"workspace.search", "workspace.run+command"}
        )
    }


def normalize_task_item(raw: Any) -> Dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    priority = 5
    interval_minutes = 30
    try:
        priority = int(src.get("priority") or 5)
    except Exception:
        priority = 5
    try:
        interval_minutes = int(src.get("interval_minutes") or 30)
    except Exception:
        interval_minutes = 30
    return {
        "id": str(src.get("id") or "").strip(),
        "title": str(src.get("title") or "未命名任务").strip() or "未命名任务",
        "instruction": str(src.get("instruction") or "").strip(),
        "priority": max(1, min(10, priority)),
        "enabled": bool(src.get("enabled", True)),
        "schedule_enabled": bool(src.get("schedule_enabled", False)),
        "interval_minutes": max(1, interval_minutes),
    }


def normalize_system_auto_control(raw: Optional[str]) -> Dict[str, Any]:
    cfg = dict(DEFAULT_SYSTEM_AUTO_CONTROL)
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                cfg.update(parsed)
        except Exception:
            pass
    cfg["enabled"] = True
    cfg["start_task_prompt"] = str(cfg.get("start_task_prompt") or DEFAULT_SYSTEM_AUTO_CONTROL["start_task_prompt"]).strip()
    cfg["resume_task_prompt"] = str(cfg.get("resume_task_prompt") or DEFAULT_SYSTEM_AUTO_CONTROL["resume_task_prompt"]).strip()
    # Clear stale supervision prompts that reference the deleted task.complete tool so
    # they fall back to the current default (which uses natural completion instead).
    if "task.complete" in str(cfg.get("supervision_prompt") or ""):
        cfg["supervision_prompt"] = ""
    cfg["supervision_prompt"] = str(cfg.get("supervision_prompt") or DEFAULT_SYSTEM_AUTO_CONTROL["supervision_prompt"]).strip()
    cfg["compression_prompt"] = str(cfg.get("compression_prompt") or DEFAULT_SYSTEM_AUTO_CONTROL["compression_prompt"]).strip()
    raw_tasks = cfg.get("tasks")
    if not isinstance(raw_tasks, list):
        raw_tasks = []
    cfg["tasks"] = [normalize_task_item(item) for item in raw_tasks]
    return cfg


def compact_system_auto_control(raw: Optional[str]) -> str:
    """Persist only per-AI controls; task-flow prompts are user-level settings."""
    try:
        parsed = json.loads(raw or "{}")
        if not isinstance(parsed, dict):
            parsed = {}
    except Exception:
        parsed = {}
    for key in TASK_FLOW_PROMPT_KEYS:
        parsed.pop(key, None)
    parsed["enabled"] = True
    raw_tasks = parsed.get("tasks")
    parsed["tasks"] = raw_tasks if isinstance(raw_tasks, list) else []
    return json.dumps(parsed, ensure_ascii=False)


def normalize_tasks_from_control(raw: Optional[str]) -> list[Dict[str, Any]]:
    return normalize_system_auto_control(raw).get("tasks", [])


def decode_task_payload(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def find_task_active_run(session: Session, user_id: int, config_id: int, job: AITaskJob) -> Optional[ChatRun]:
    run_row = None
    if job.last_run_id:
        run_row = session.exec(
            select(ChatRun).where(
                ChatRun.user_id == user_id,
                ChatRun.run_id == job.last_run_id,
            )
        ).first()
        if run_row and run_row.status in {"queued", "running"}:
            return run_row
    if job.session_id:
        run_row = session.exec(
            select(ChatRun).where(
                ChatRun.user_id == user_id,
                ChatRun.ai_config_id == config_id,
                ChatRun.ai_kind == "core",
                ChatRun.session_id == job.session_id,
                ChatRun.status.in_(["queued", "running"]),
            ).order_by(ChatRun.updated_at.desc())
        ).first()
        if run_row:
            return run_row
    return None


def iter_task_session_ids(job_id: str, current_session_id: Optional[str]) -> list[str]:
    prefix = f"session_task_{job_id}"
    out = [prefix]
    sid = str(current_session_id or "").strip()
    if sid and not sid.startswith(prefix) and sid not in out:
        out.append(sid)
    return out


def _parse_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _parse_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def extract_task_payload(body: Dict[str, Any]) -> Dict[str, Any]:
    # schedule 的解析/校验/补全统一走 task_schedule 模块（唯一权威实现）
    from .task_schedule import extract_schedule, finalize_schedule

    schedule = finalize_schedule(extract_schedule(body))

    override_token_limit_enabled = _parse_bool(body.get("override_token_limit_enabled"), False)
    token_limit_override = _parse_int(body.get("token_limit_override"), 10000, 1, 10**9)

    override_mcp_tools_enabled = _parse_bool(body.get("override_mcp_tools_enabled"), False)
    mcp_tools_raw = body.get("mcp_tools_override")
    if isinstance(mcp_tools_raw, str):
        try:
            mcp_tools_raw = json.loads(mcp_tools_raw)
        except Exception:
            mcp_tools_raw = []
    mcp_tools_override: list[str] = []
    if isinstance(mcp_tools_raw, list):
        dedup = set()
        for tool in mcp_tools_raw:
            item = str(tool or "").strip()
            if not item or item in dedup:
                continue
            dedup.add(item)
            mcp_tools_override.append(item)

    return {
        "schedule": schedule,
        "override_token_limit": {
            "enabled": override_token_limit_enabled,
            "value": token_limit_override,
        },
        "override_mcp_tools": {
            "enabled": override_mcp_tools_enabled,
            "tools": mcp_tools_override,
        },
        "override_workspace_root": {
            "enabled": False,
            "value": "",
        },
    }
