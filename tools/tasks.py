import json
import os
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from api.database import engine
from api.models import AITaskJob, AssistantAIConfig, ChatMessage, ChatRun, ChatSession
from api.services.chat.chat_media import delete_message_media
from connector_runtime.dispatch.device_dispatch import get_run_session_context
from api.services.tasks import task_plan as plan_service
from api.services.tasks.task_schedule import (
    AT_KEYS as _SCHEDULE_AT_KEYS,
    DAILY_TIME_KEYS as _SCHEDULE_DAILY_TIME_KEYS,
    DURATION_KEYS as _SCHEDULE_DURATION_KEYS,
    END_AT_KEYS as _SCHEDULE_END_AT_KEYS,
    LOOP_MODE_KEYS as _SCHEDULE_LOOP_MODE_KEYS,
    LOOP_MODES,
    MAX_RUNS_KEYS as _SCHEDULE_MAX_RUNS_KEYS,
    WEEKLY_DAYS_KEYS as _SCHEDULE_WEEKLY_DAYS_KEYS,
    describe_schedule,
    finalize_schedule,
    normalize_schedule,
    parse_daily_time,
    parse_timestamp_strict,
    parse_weekly_days,
)
from api.services.tasks.task_system import extract_task_payload
from api.common.value_utils import safe_json_obj, to_bool
from mcp_runtime.mcp.core import get_project_root

_FINISHED_STATUSES = {"completed", "cancelled", "stopped", "error"}
_ACTIVE_STATUSES = {"queued", "running", "paused"}
_TASK_LIST_STATUSES = _ACTIVE_STATUSES | _FINISHED_STATUSES

# Phase 5: concurrency caps (0 = unlimited)
_MAX_ACTIVE_TASKS_PER_AI = 10
_MAX_ACTIVE_SUBTASKS_PER_MANAGER = 20





def _pick_value(source: Dict[str, Any], keys: tuple[str, ...]) -> Any:
    if not isinstance(source, dict):
        return None
    for key in keys:
        if key in source:
            return source.get(key)
    return None

def _task_priority_from_args(args: Dict[str, Any]) -> int:
    raw = args.get("priority")
    if raw is None:
        raw = args.get("level")
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        level_map = {
            "high": 8,
            "medium": 5,
            "low": 3,
            "urgent": 10,
            "critical": 10,
            "normal": 5,
            "高": 8,
            "中": 5,
            "低": 3,
            "紧急": 10,
            "普通": 5,
        }
        if normalized in level_map:
            return int(level_map[normalized])
    try:
        parsed = int(raw)
    except Exception:
        parsed = 5
    return max(1, min(10, parsed))

def _pick_schedule_value(args: Dict[str, Any], keys: tuple[str, ...]) -> Any:
    schedule_raw = args.get("schedule")
    schedule_obj = schedule_raw if isinstance(schedule_raw, dict) else {}
    local = _pick_value(schedule_obj, keys)
    if local is not None:
        return local
    return _pick_value(args, keys)

def _is_non_empty_schedule_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True

def _override_schedule_arg(args: Dict[str, Any], key: str, value: Any) -> Dict[str, Any]:
    """把解析后的值写回 args（含嵌套 schedule 对象），覆盖原始别名输入。"""
    out = dict(args or {})
    out[key] = value
    schedule_raw = out.get("schedule")
    if isinstance(schedule_raw, dict):
        schedule_obj = dict(schedule_raw)
        schedule_obj[key] = value
        out["schedule"] = schedule_obj
    return out

def _resolve_schedule_run_immediately(args: Dict[str, Any], default: bool = False) -> bool:
    raw = _pick_schedule_value(
        args,
        (
            "run_immediately",
            "schedule_run_immediately",
            "first_run_immediately",
            "immediate",
            "run_now",
        ),
    )
    return to_bool(raw, default)

def _build_task_payload_from_args(args: Dict[str, Any]) -> tuple[Dict[str, Any], bool]:
    """构建任务 payload；schedule 的别名兼容、hint 推断与 schedule_at 补全
    统一由 extract_task_payload → task_schedule 模块完成。"""
    task_payload = extract_task_payload(args if isinstance(args, dict) else {})
    schedule = task_payload.get("schedule")
    schedule_enabled = bool(isinstance(schedule, dict) and schedule.get("enabled"))
    return task_payload, schedule_enabled

def _enforce_task_schedule_mode(
    task_payload: Dict[str, Any],
    *,
    enabled: bool,
    loop_enabled: bool,
    run_immediately: bool,
) -> None:
    """按 mode 强制覆盖定时开关，再统一重算 schedule_at。"""
    schedule = normalize_schedule(task_payload.get("schedule"))
    schedule["enabled"] = bool(enabled)
    schedule["loop_enabled"] = bool(enabled and loop_enabled)
    schedule["run_immediately"] = bool(enabled and loop_enabled and run_immediately)
    task_payload["schedule"] = finalize_schedule(schedule)

def _task_create_impl(
    user_id: int,
    args: Dict[str, Any],
    ai_config_id: Optional[int],
    *,
    source_tool: str,
    mode: str,
) -> Dict[str, Any]:
    title = str(args.get("title") or args.get("name") or args.get("task_name") or "").strip()
    instruction = str(args.get("instruction") or args.get("content") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail=f"title (or name/task_name) is required for {source_tool}")
    if not instruction:
        raise HTTPException(status_code=400, detail=f"instruction (or content) is required for {source_tool}")

    normalized_args = dict(args or {})
    raw_schedule_at = _pick_schedule_value(normalized_args, _SCHEDULE_AT_KEYS)
    raw_schedule_duration = _pick_schedule_value(normalized_args, _SCHEDULE_DURATION_KEYS)
    parsed_schedule_at, schedule_at_error, has_schedule_at_input = parse_timestamp_strict(raw_schedule_at)
    has_duration_input = _is_non_empty_schedule_value(raw_schedule_duration)
    if schedule_at_error:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{source_tool}: schedule_at {schedule_at_error}。"
                "仅支持 Unix 秒时间戳，或带时区 ISO-8601（例如 2026-03-24T16:30:00+08:00）。"
            ),
        )
    if has_schedule_at_input and parsed_schedule_at is not None:
        normalized_args = _override_schedule_arg(normalized_args, "schedule_at", parsed_schedule_at)

    # 循环专属参数（仅 mode=recurring 接受）
    raw_loop_mode = _pick_schedule_value(normalized_args, _SCHEDULE_LOOP_MODE_KEYS)
    raw_daily_time = _pick_schedule_value(normalized_args, _SCHEDULE_DAILY_TIME_KEYS)
    raw_weekly_days = _pick_schedule_value(normalized_args, _SCHEDULE_WEEKLY_DAYS_KEYS)
    raw_max_runs = _pick_schedule_value(normalized_args, _SCHEDULE_MAX_RUNS_KEYS)
    raw_end_at = _pick_schedule_value(normalized_args, _SCHEDULE_END_AT_KEYS)
    has_loop_param = any(
        _is_non_empty_schedule_value(item)
        for item in (raw_loop_mode, raw_daily_time, raw_weekly_days, raw_max_runs, raw_end_at)
    )
    parsed_end_at, end_at_error, has_end_at_input = parse_timestamp_strict(raw_end_at)
    if end_at_error:
        raise HTTPException(status_code=400, detail=f"{source_tool}: schedule_end_at {end_at_error}。")
    if has_end_at_input and parsed_end_at is not None:
        normalized_args = _override_schedule_arg(normalized_args, "schedule_end_at", parsed_end_at)

    if mode == "immediate":
        if has_schedule_at_input or has_duration_input or has_loop_param:
            raise HTTPException(
                status_code=400,
                detail=f"{source_tool}: mode=immediate 不接受任何定时/循环参数，请移除 schedule_* 字段。",
            )
    elif mode == "scheduled":
        if has_loop_param:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{source_tool}: mode=scheduled 不接受循环参数"
                    "（schedule_loop_mode/schedule_daily_time/schedule_weekly_days/schedule_max_runs/schedule_end_at），"
                    "循环请使用 mode=recurring。"
                ),
            )
        if has_schedule_at_input and has_duration_input:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{source_tool}: schedule_at 与 schedule_duration_minutes 只能二选一，"
                    "不要同时传入。"
                ),
            )
        if (not has_schedule_at_input) and (not has_duration_input):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{source_tool}: 请明确提供 schedule_at 或 schedule_duration_minutes 其一，"
                    "避免默认时间导致错位执行。"
                ),
            )
    elif mode == "recurring":
        if has_schedule_at_input:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{source_tool}: mode=recurring 不支持 schedule_at；"
                    "请用 schedule_loop_mode 选择循环方式（interval/daily/weekly）。"
                ),
            )
        loop_mode = str(raw_loop_mode or "interval").strip().lower()
        if loop_mode not in LOOP_MODES:
            raise HTTPException(
                status_code=400,
                detail=f"{source_tool}: schedule_loop_mode 必须是 interval、daily 或 weekly。",
            )
        if loop_mode in {"daily", "weekly"} and parse_daily_time(raw_daily_time) is None:
            raise HTTPException(
                status_code=400,
                detail=f"{source_tool}: loop_mode={loop_mode} 需要 schedule_daily_time（HH:MM，服务器本地时区）。",
            )
        if loop_mode == "weekly" and not parse_weekly_days(raw_weekly_days):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{source_tool}: loop_mode=weekly 需要 schedule_weekly_days，"
                    "如 [0,2,4]（0=周一 ... 6=周日）。"
                ),
            )
        if loop_mode == "interval" and _is_non_empty_schedule_value(raw_daily_time):
            raise HTTPException(
                status_code=400,
                detail=f"{source_tool}: schedule_daily_time 仅在 loop_mode=daily/weekly 时有效。",
            )
        normalized_args = _override_schedule_arg(normalized_args, "schedule_loop_mode", loop_mode)
    else:
        raise HTTPException(
            status_code=400,
            detail=f"{source_tool}: mode 必须是 immediate、scheduled 或 recurring。",
        )

    priority = _task_priority_from_args(normalized_args)
    task_payload, schedule_enabled = _build_task_payload_from_args(normalized_args)
    template_id = str(normalized_args.get("template_id") or "").strip() or None

    if mode == "immediate":
        _enforce_task_schedule_mode(
            task_payload,
            enabled=False,
            loop_enabled=False,
            run_immediately=False,
        )
        schedule_enabled = False
    elif mode == "scheduled":
        _enforce_task_schedule_mode(
            task_payload,
            enabled=True,
            loop_enabled=False,
            run_immediately=False,
        )
        schedule_enabled = True
    elif mode == "recurring":
        _enforce_task_schedule_mode(
            task_payload,
            enabled=True,
            loop_enabled=True,
            run_immediately=_resolve_schedule_run_immediately(normalized_args, False),
        )
        schedule_enabled = True

    run_ctx = get_run_session_context() or {}
    created_by_session_id = str(run_ctx.get("session_id") or "").strip() or None

    with Session(engine) as session:
        owner_cfg = _resolve_task_runtime_owner(session, user_id, ai_config_id, normalized_args)

        is_fan_out = ai_config_id and int(owner_cfg.id) != int(ai_config_id)

        # Per-AI concurrency cap
        if _MAX_ACTIVE_TASKS_PER_AI > 0:
            active_count = session.exec(
                select(AITaskJob).where(
                    AITaskJob.user_id == user_id,
                    AITaskJob.ai_config_id == int(owner_cfg.id),
                    AITaskJob.status.notin_(list(_FINISHED_STATUSES)),
                )
            ).all()
            if len(active_count) >= _MAX_ACTIVE_TASKS_PER_AI:
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"AI '{owner_cfg.name}' already has {len(active_count)} active tasks "
                        f"(cap={_MAX_ACTIVE_TASKS_PER_AI}). "
                        "Wait for existing tasks to complete before dispatching more."
                    ),
                )

        # Per-manager fan-out subtask cap
        if is_fan_out and _MAX_ACTIVE_SUBTASKS_PER_MANAGER > 0:
            dispatched_count = session.exec(
                select(AITaskJob).where(
                    AITaskJob.user_id == user_id,
                    AITaskJob.created_by_ai_config_id == int(ai_config_id),
                    AITaskJob.status.notin_(list(_FINISHED_STATUSES)),
                )
            ).all()
            if len(dispatched_count) >= _MAX_ACTIVE_SUBTASKS_PER_MANAGER:
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"Manager AI has {len(dispatched_count)} active dispatched subtasks "
                        f"(cap={_MAX_ACTIVE_SUBTASKS_PER_MANAGER}). "
                        "Wait for subtasks to complete before dispatching more."
                    ),
                )

        row = AITaskJob(
            job_id=f"job_{uuid.uuid4().hex[:12]}",
            user_id=user_id,
            ai_config_id=int(owner_cfg.id),
            created_by_ai_config_id=int(ai_config_id) if ai_config_id else None,
            created_by_session_id=created_by_session_id,
            ai_kind="core",
            template_id=template_id,
            title=title,
            instruction=instruction,
            task_payload=json.dumps(task_payload, ensure_ascii=False),
            priority=priority,
            status="queued",
            trigger_type="schedule" if schedule_enabled else "manual",
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return {
            "created": True,
            "job_id": row.job_id,
            "title": row.title,
            "priority": row.priority,
            "status": row.status,
            "owner_ai_config_id": owner_cfg.id,
            "owner_ai_name": owner_cfg.name,
            "created_at": _format_ts_local(_safe_timestamp(row.created_at)),
            "schedule": _build_task_schedule_meta(task_payload),
        }

def _safe_timestamp(value: Any) -> Optional[float]:
    try:
        ts = float(value)
    except Exception:
        return None
    if ts <= 0:
        return None
    return ts

def _format_ts_local(ts: Optional[float]) -> str:
    if ts is None:
        return ""
    from api.services.tasks.task_schedule import schedule_timezone

    return datetime.fromtimestamp(float(ts), schedule_timezone()).isoformat(sep=" ", timespec="seconds")

def _build_task_schedule_meta(task_payload: Dict[str, Any]) -> Dict[str, Any]:
    """精简的调度说明：只暴露 AI 真正需要的字段。

    非定时任务返回 ``{"enabled": False}``；定时任务给一句人读摘要、下次执行时间，
    以及后续可用来改写调度的参数。不再返回 unix/utc 等冗余时间格式。
    """
    raw = task_payload.get("schedule") if isinstance(task_payload, dict) else {}
    schedule = normalize_schedule(raw)
    if not schedule["enabled"]:
        return {"enabled": False}
    meta: Dict[str, Any] = {
        "enabled": True,
        "summary": describe_schedule(schedule),
        "next_run_at": _format_ts_local(_safe_timestamp(schedule.get("schedule_at"))),
        "loop_enabled": schedule["loop_enabled"],
    }
    if schedule["loop_enabled"]:
        loop_mode = schedule["loop_mode"]
        meta["loop_mode"] = loop_mode
        if loop_mode == "interval":
            meta["interval_minutes"] = schedule["duration_minutes"]
        if loop_mode in {"daily", "weekly"}:
            meta["daily_time"] = schedule["daily_time"]
        if loop_mode == "weekly":
            meta["weekly_days"] = schedule["weekly_days"]
        if schedule["max_runs"]:
            meta["max_runs"] = schedule["max_runs"]
            meta["runs_done"] = schedule["runs_done"]
        end_at = _safe_timestamp(schedule.get("end_at"))
        if end_at:
            meta["end_at"] = _format_ts_local(end_at)
    return meta

def _task_job_payload(row: AITaskJob) -> Dict[str, Any]:
    payload = safe_json_obj(row.task_payload)
    return {
        "job_id": row.job_id,
        "title": row.title,
        "instruction": row.instruction,
        "priority": row.priority,
        "status": row.status,
        "trigger_type": row.trigger_type,
        "session_id": row.session_id,
        "created_at": _format_ts_local(_safe_timestamp(row.created_at)),
        "schedule": _build_task_schedule_meta(payload),
    }

def _resolve_task_runtime_owner(
    session: Session,
    user_id: int,
    ai_config_id: Optional[int],
    args: Dict[str, Any],
) -> AssistantAIConfig:
    if not ai_config_id:
        raise HTTPException(status_code=400, detail="ai_config_id is required for task tools")

    caller_cfg = session.exec(
        select(AssistantAIConfig).where(
            AssistantAIConfig.user_id == user_id,
            AssistantAIConfig.id == ai_config_id,
        )
    ).first()
    if not caller_cfg:
        raise HTTPException(status_code=404, detail="AI config not found")

    caller_role = str(caller_cfg.ai_role or "").strip()
    raw_target = args.get("target_ai_config_id")
    if raw_target is None:
        raw_target = args.get("target_config_id")

    def _resolve_target(target_raw: Any) -> AssistantAIConfig:
        try:
            target_id = int(target_raw)
        except Exception:
            raise HTTPException(status_code=400, detail="target_ai_config_id must be an integer")
        target_cfg = session.exec(
            select(AssistantAIConfig).where(
                AssistantAIConfig.user_id == user_id,
                AssistantAIConfig.id == target_id,
            )
        ).first()
        if not target_cfg:
            raise HTTPException(status_code=404, detail="Target AI config not found")
        if str(target_cfg.ai_role or "").strip() != "digital_member":
            raise HTTPException(status_code=400, detail="Target AI config must be digital_member")
        return target_cfg

    if caller_role == "digital_member":
        # 图书馆绑定是唯一权限门槛；普通成员也可把任务投递给账号内任意数字成员。
        if raw_target is not None:
            return _resolve_target(raw_target)
        return caller_cfg

    if caller_role != "assistant_admin":
        raise HTTPException(status_code=400, detail="Only digital_member or assistant_admin supports task scheduler")

    if raw_target is not None:
        return _resolve_target(raw_target)

    candidates = session.exec(
        select(AssistantAIConfig).where(
            AssistantAIConfig.user_id == user_id,
            AssistantAIConfig.ai_role == "digital_member",
        ).order_by(AssistantAIConfig.sort_order.asc(), AssistantAIConfig.created_at.asc())
    ).all()
    if not candidates:
        raise HTTPException(
            status_code=400,
            detail="No digital_member available for task scheduling; provide target_ai_config_id or create one",
        )
    manager = next(
        (cfg for cfg in candidates if str(cfg.digital_member_role or "").strip().lower() == "manager"),
        None,
    )
    return manager or candidates[0]

def _load_task_job_for_owner(
    session: Session,
    user_id: int,
    owner_cfg: AssistantAIConfig,
    job_id: str,
) -> AITaskJob:
    if not job_id:
        raise HTTPException(status_code=400, detail="job_id is required")
    row = session.exec(
        select(AITaskJob).where(
            AITaskJob.user_id == user_id,
            AITaskJob.ai_config_id == int(owner_cfg.id),
            AITaskJob.job_id == job_id,
        )
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Task job not found")
    return row

def _merge_task_payload_for_update(existing_payload: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(existing_payload or {})
    has_schedule_update = any(
        key in args
        for key in (
            "mode",
            "schedule_enabled",
            "schedule_at",
            "run_at",
            "schedule_time",
            "schedule_duration_minutes",
            "duration_minutes",
            "interval_minutes",
            "schedule_loop_enabled",
            "loop",
            "repeat",
            "schedule_run_immediately",
            "run_now",
            "schedule_loop_mode",
            "loop_mode",
            "schedule_daily_time",
            "daily_time",
            "schedule_weekly_days",
            "weekly_days",
            "schedule_max_runs",
            "max_runs",
            "schedule_end_at",
            "end_at",
            "schedule",
        )
    )
    if has_schedule_update:
        mode = str(args.get("mode") or "").strip().lower()
        if mode in {"now", "manual"}:
            mode = "immediate"
        elif mode in {"once", "schedule"}:
            mode = "scheduled"
        elif mode in {"loop", "repeat"}:
            mode = "recurring"
        patch_payload, _ = _build_task_payload_from_args(args)
        schedule = patch_payload.get("schedule") if isinstance(patch_payload, dict) else {}
        if not isinstance(schedule, dict):
            schedule = {}
        if mode == "immediate":
            _enforce_task_schedule_mode(schedule_payload := {"schedule": schedule}, enabled=False, loop_enabled=False, run_immediately=False)
            schedule = schedule_payload["schedule"]
        elif mode == "scheduled":
            _enforce_task_schedule_mode(schedule_payload := {"schedule": schedule}, enabled=True, loop_enabled=False, run_immediately=False)
            schedule = schedule_payload["schedule"]
        elif mode == "recurring":
            _enforce_task_schedule_mode(
                schedule_payload := {"schedule": schedule},
                enabled=True,
                loop_enabled=True,
                run_immediately=_resolve_schedule_run_immediately(args, False),
            )
            schedule = schedule_payload["schedule"]
        elif mode:
            raise HTTPException(status_code=400, detail="mode must be immediate, scheduled, or recurring")
        payload["schedule"] = schedule
    return payload

def _task_create(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    normalized_args = args if isinstance(args, dict) else {}
    mode = str(normalized_args.get("mode") or "").strip().lower()
    mode_aliases = {
        "now": "immediate",
        "manual": "immediate",
        "once": "scheduled",
        "schedule": "scheduled",
        "loop": "recurring",
        "repeat": "recurring",
    }
    mode = mode_aliases.get(mode, mode)
    if not mode:
        schedule_raw = normalized_args.get("schedule")
        schedule_obj = schedule_raw if isinstance(schedule_raw, dict) else {}
        has_loop_hint = any(
            _is_non_empty_schedule_value(_pick_schedule_value(normalized_args, keys))
            for keys in (
                _SCHEDULE_LOOP_MODE_KEYS,
                _SCHEDULE_DAILY_TIME_KEYS,
                _SCHEDULE_WEEKLY_DAYS_KEYS,
                _SCHEDULE_MAX_RUNS_KEYS,
                _SCHEDULE_END_AT_KEYS,
            )
        ) or to_bool(
            _pick_value(schedule_obj, ("loop_enabled", "loop", "repeat")),
            False,
        )
        has_schedule_hint = any(
            _is_non_empty_schedule_value(_pick_schedule_value(normalized_args, keys))
            for keys in (_SCHEDULE_AT_KEYS, _SCHEDULE_DURATION_KEYS)
        ) or to_bool(
            _pick_value(schedule_obj, ("enabled", "schedule_enabled")),
            False,
        )
        mode = "recurring" if has_loop_hint else ("scheduled" if has_schedule_hint else "immediate")
    return _task_create_impl(user_id, normalized_args, ai_config_id, source_tool="task.create", mode=mode)

def _task_update(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    job_id = str(args.get("job_id") or "").strip()
    with Session(engine) as session:
        owner_cfg = _resolve_task_runtime_owner(session, user_id, ai_config_id, args)
        row = _load_task_job_for_owner(session, user_id, owner_cfg, job_id)
        previous_status = str(row.status or "")

        title_provided = "title" in args or "name" in args or "task_name" in args
        instruction_provided = "instruction" in args or "content" in args
        if title_provided:
            title = str(args.get("title") or args.get("name") or args.get("task_name") or "").strip()
            if not title:
                raise HTTPException(status_code=400, detail="title cannot be empty")
            row.title = title
        if instruction_provided:
            instruction = str(args.get("instruction") or args.get("content") or "").strip()
            if not instruction:
                raise HTTPException(status_code=400, detail="instruction cannot be empty")
            row.instruction = instruction
        if "priority" in args or "level" in args:
            row.priority = _task_priority_from_args(args)
        if "status" in args:
            status = str(args.get("status") or "").strip().lower()
            if status not in {"queued", "paused"}:
                raise HTTPException(status_code=400, detail="status can only be queued or paused")
            if previous_status in _FINISHED_STATUSES:
                raise HTTPException(status_code=400, detail="Cannot update a finished task status")
            row.status = status
            if status == "queued":
                row.finished_at = None

        payload = _merge_task_payload_for_update(safe_json_obj(row.task_payload), args)
        row.task_payload = json.dumps(payload, ensure_ascii=False)
        schedule = payload.get("schedule") if isinstance(payload, dict) else {}
        row.trigger_type = "schedule" if isinstance(schedule, dict) and bool(schedule.get("enabled")) else "manual"
        row.updated_at = time.time()
        session.add(row)
        session.commit()
        session.refresh(row)
        return {
            "updated": True,
            "previous_status": previous_status,
            "owner_ai_name": owner_cfg.name,
            "task": _task_job_payload(row),
            "runtime_note": "若任务正在运行，标题/说明的修改只影响持久化元数据，不会改写当前运行中的提示。",
        }

def _delete_task_job_records(session: Session, user_id: int, config_id: int, job: AITaskJob) -> int:
    deleted_messages = 0
    now = time.time()
    prefixes = [f"session_task_{job.job_id}"]
    sid = str(job.session_id or "").strip()
    if sid and sid not in prefixes:
        prefixes.append(sid)

    for session_prefix in prefixes:
        run_rows = session.exec(
            select(ChatRun).where(
                ChatRun.user_id == user_id,
                ChatRun.ai_config_id == config_id,
                ChatRun.ai_kind == "core",
                ChatRun.session_id.like(f"{session_prefix}%"),
            )
        ).all()
        for row in run_rows:
            row.stop_requested = True
            if row.status in {"queued", "running"}:
                row.status = "stopped"
            if row.finished_at is None:
                row.finished_at = now
            row.updated_at = now
            session.add(row)

        msg_rows = session.exec(
            select(ChatMessage).where(
                ChatMessage.user_id == user_id,
                ChatMessage.ai_config_id == config_id,
                ChatMessage.ai_kind == "core",
                ChatMessage.session_id.like(f"{session_prefix}%"),
            )
        ).all()
        deleted_messages += len(msg_rows)
        delete_message_media(session, msg_rows)
        for msg in msg_rows:
            session.delete(msg)

        session_rows = session.exec(
            select(ChatSession).where(
                ChatSession.user_id == user_id,
                ChatSession.ai_config_id == config_id,
                ChatSession.ai_kind == "core",
                ChatSession.session_id.like(f"{session_prefix}%"),
            )
        ).all()
        for row in session_rows:
            session.delete(row)
    return deleted_messages

def _task_delete(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    job_id = str(args.get("job_id") or "").strip()
    with Session(engine) as session:
        owner_cfg = _resolve_task_runtime_owner(session, user_id, ai_config_id, args)
        row = _load_task_job_for_owner(session, user_id, owner_cfg, job_id)
        previous_status = str(row.status or "")
        deleted_messages = _delete_task_job_records(session, user_id, int(owner_cfg.id), row)
        session.delete(row)
        session.commit()
        return {
            "deleted": True,
            "job_id": job_id,
            "previous_status": previous_status,
            "deleted_messages": deleted_messages,
            "owner_ai_name": owner_cfg.name,
        }

def _task_status_rank(raw: str) -> int:
    status = str(raw or "").strip()
    if status == "running":
        return 0
    if status == "queued":
        return 1
    if status == "paused":
        return 2
    return 9

def _parse_task_list_statuses(value: Any) -> List[str]:
    if value is None:
        return []
    raw_items = value if isinstance(value, list) else str(value).split(",")
    out: List[str] = []
    seen = set()
    for raw in raw_items:
        item = str(raw or "").strip()
        if not item or item not in _TASK_LIST_STATUSES or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out

def _parse_task_list_limit(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(1, min(500, parsed))

def _task_row_to_dict(row: AITaskJob) -> Dict[str, Any]:
    out = _task_job_payload(row)
    started = _format_ts_local(_safe_timestamp(row.started_at))
    finished = _format_ts_local(_safe_timestamp(row.finished_at))
    if started:
        out["started_at"] = started
    if finished:
        out["finished_at"] = finished
    return out

def _task_list(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    job_id = str(args.get("job_id") or "").strip()
    current_only = to_bool(args.get("current_only", args.get("current")), False)
    include_history = to_bool(args.get("include_history", args.get("history")), False)
    history_only = to_bool(args.get("history_only"), False)
    requested_statuses = _parse_task_list_statuses(args.get("status") or args.get("statuses"))
    limit = _parse_task_list_limit(args.get("limit"), 100 if (include_history or history_only) else 500)
    with Session(engine) as session:
        owner_cfg = _resolve_task_runtime_owner(session, user_id, ai_config_id, args)
        query = select(AITaskJob).where(
            AITaskJob.user_id == user_id,
            AITaskJob.ai_config_id == int(owner_cfg.id),
        )
        if job_id:
            query = query.where(AITaskJob.job_id == job_id)
        elif requested_statuses:
            query = query.where(AITaskJob.status.in_(requested_statuses))
        elif history_only:
            query = query.where(AITaskJob.status.in_(list(_FINISHED_STATUSES)))
        elif not include_history:
            query = query.where(AITaskJob.status.in_(list(_ACTIVE_STATUSES)))
        rows = session.exec(
            query.order_by(AITaskJob.priority.desc(), AITaskJob.created_at.asc())
        ).all()
        if current_only or job_id:
            rows.sort(
                key=lambda item: (
                    _task_status_rank(str(item.status or "")),
                    -int(item.priority or 0),
                    float(item.created_at or 0),
                )
            )
            rows = rows[:1]
        elif include_history or history_only or requested_statuses:
            rows.sort(
                key=lambda item: (
                    0 if str(item.status or "") in _ACTIVE_STATUSES else 1,
                    _task_status_rank(str(item.status or "")),
                    -float(item.created_at or 0),
                )
            )
            rows = rows[:limit]
        tasks = [_task_row_to_dict(row) for row in rows]
        task = tasks[0] if tasks else None
        return {
            "owner_ai_config_id": owner_cfg.id,
            "owner_ai_name": owner_cfg.name,
            "count": len(tasks),
            "task": task if (current_only or job_id) else None,
            "tasks": tasks,
        }

def _task_manage(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    """Unified task management tool. Dispatch by ``action``.

    Folds ``task.{create,list,update,delete}`` behind one ``action`` parameter.
    The phased todo flow (todo.manage create/edit/delete)
    is separate because the task runtime drives the plan boundary.
    Library binding is the only authorization boundary; bound members may use
    every action regardless of role.
    """
    raw = str((args or {}).get("action") or "").strip().lower()
    action = _TASK_ACTION_ALIASES.get(raw, raw)
    if not action:
        raise HTTPException(status_code=400, detail="action is required for task.manage")
    spec = _TASK_ACTIONS.get(action)
    if spec is None:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported action: {action}. 可用: {', '.join(sorted(_TASK_ACTIONS))}",
        )
    return spec(user_id, args or {}, ai_config_id)


_TASK_ACTIONS = {
    "list": _task_list,
    "create": _task_create,
    "update": _task_update,
    "delete": _task_delete,
}

_TASK_ACTION_ALIASES = {
    "add": "create",
    "new": "create",
    "edit": "update",
    "remove": "delete",
}

TASK_MANAGE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": sorted(_TASK_ACTIONS),
            "description": (
                "操作类型："
                "list 列出任务（默认进行中；current_only/include_history/history_only/status 可调整范围）；"
                "create 创建任务（支持 immediate/scheduled/recurring）；"
                "update 接管更新任务标题/说明/优先级/状态/调度；"
                "delete 彻底删除任务并清理其会话。"
                "注意：长动作分阶段执行统一用 todo.manage；先 knowledge.search 检索经验，再 action=create 建计划，阶段结束用 action=edit 更新，最后阶段更新后系统自动收尾。"
            ),
        },
        # ---- list ----
        "current_only": {"type": "boolean", "description": "list：只返回当前任务（优先运行中，其次排队，再次暂停）。"},
        "include_history": {"type": "boolean", "description": "list：在进行中任务之外附带已结束的历史任务。"},
        "history_only": {"type": "boolean", "description": "list：只返回已结束的历史任务。"},
        "status": {
            "description": "list：按状态过滤（单个或逗号分隔/数组）；update：接管后的状态，仅支持 queued 或 paused。",
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ],
        },
        "limit": {"type": "integer", "description": "list：历史/状态过滤时的最大条数，1-500。"},
        # ---- create / update shared ----
        "job_id": {"type": "string", "description": "update/delete 必填：目标任务 job id。"},
        "title": {"type": "string", "description": "create 必填 / update 可选：任务标题。"},
        "instruction": {"type": "string", "description": "create 必填 / update 可选：任务执行说明。"},
        "priority": {"type": "integer", "description": "优先级 1-10，默认 5。"},
        "mode": {
            "type": "string",
            "enum": ["immediate", "scheduled", "recurring"],
            "description": "create/update：任务类型。immediate=立即执行，scheduled=一次性定时，recurring=循环运行。",
        },
        "schedule_at": {"type": ["number", "string"], "description": "scheduled：执行时间，Unix 秒或带时区 ISO-8601。"},
        "schedule_duration_minutes": {"type": "integer", "description": "scheduled: now + 分钟数；recurring(interval): 循环间隔分钟。"},
        "schedule_loop_mode": {
            "type": "string",
            "enum": ["interval", "daily", "weekly"],
            "description": "recurring 循环方式：interval/daily/weekly。",
        },
        "schedule_daily_time": {"type": "string", "description": "daily/weekly 循环触发时刻 HH:MM（服务器本地时区）。"},
        "schedule_weekly_days": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "weekly 循环的星期列表，0=周一 ... 6=周日。",
        },
        "schedule_max_runs": {"type": "integer", "description": "循环总轮数上限，0/省略=不限。"},
        "schedule_end_at": {"type": ["number", "string"], "description": "循环截止时间，Unix 秒或带时区 ISO-8601。"},
        "schedule_run_immediately": {"type": "boolean", "description": "recurring 是否首轮立即执行。"},
        "template_id": {"type": "string", "description": "create：可选模板 id。"},
        "target_ai_config_id": {"type": "integer", "description": "可选，投递任务的目标数字成员 AI 配置 id。"},
    },
    "required": ["action"],
}
