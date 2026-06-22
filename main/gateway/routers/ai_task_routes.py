"""``/api/ai`` task routes: trigger AI tasks and manage task jobs (list, inspect,
patch, stop/pause/resume, delete) for a given AI config."""

IS_ROUTER_ENTRY = False

import json
import time
import uuid

from fastapi import Depends, Header, HTTPException
from sqlmodel import Session, select

from api.database import get_session
from api.models import AITaskJob, ChatMessage, ChatRun, ChatSession
from api.services.access_guards import get_ai_config_or_404
from api.services.chat_media import delete_message_media
from .auth import get_current_user
from api.services.task_system import (
    decode_task_payload,
    extract_task_payload,
    find_task_active_run,
    iter_task_session_ids,
    normalize_tasks_from_control,
)
from .ai_base import (
    _append_task_title_suffix,
    _resolve_task_owner_cfg,
    router,
)


@router.get("/configs/{config_id}/plan")
async def get_ai_task_plan(
    config_id: int,
    session_id: str = "",
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    """Plan progress for a chat session, for the web task-mode progress UI.

    Returns the coarse ``stage`` (planning/executing/finishing/finished/none)
    plus the full plan + phases (each with sub-actions and status) so the
    console can render: 安排 → 实施的阶段 N → 子任务进度 → 结束.
    """
    from api.services import task_plan as plan_service

    user = get_current_user(authorization, session)
    cfg = get_ai_config_or_404(session, config_id, user.id)
    sid = str(session_id or "").strip()
    is_task_session = bool(sid.startswith("session_task_"))
    plan = plan_service.get_session_plan(session, user.id, int(cfg.id), sid) if sid else None
    if plan is not None:
        is_task_session = True
    stage = plan_service.progress_stage(session, plan, is_task_session=is_task_session)
    payload = {
        "is_task_session": is_task_session,
        "stage": stage,
        "has_plan": plan is not None,
        "outcome": (plan.outcome or "") if plan is not None else "",
        "plan": plan_service.plan_progress(session, plan) if plan is not None else None,
    }
    return payload


@router.post("/configs/{config_id}/task-trigger")
async def trigger_ai_task(
    config_id: int,
    body: dict,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    cfg = get_ai_config_or_404(session, config_id, user.id)

    payload_body = body if isinstance(body, dict) else {}
    owner_cfg = _resolve_task_owner_cfg(session, user.id, cfg, payload_body)
    template_id = str(payload_body.get("template_id") or "").strip()
    tasks = normalize_tasks_from_control(owner_cfg.system_auto_control)
    chosen = None
    if template_id:
        chosen = next((t for t in tasks if t.get("id") == template_id), None)
    if not chosen:
        title = str(payload_body.get("title") or "").strip()
        instruction = str(payload_body.get("instruction") or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="template_id or title is required")
        chosen = {
            "id": template_id or "",
            "title": title,
            "instruction": instruction,
            "priority": max(1, min(10, int(payload_body.get("priority") or 5))),
        }
    # schedule 的解析与 schedule_at 补全已统一在 extract_task_payload 内完成
    task_payload = extract_task_payload(payload_body)
    schedule_cfg = task_payload.get("schedule")
    schedule_enabled = bool(isinstance(schedule_cfg, dict) and schedule_cfg.get("enabled"))
    task_title = _append_task_title_suffix(str(chosen.get("title") or ""))

    row = AITaskJob(
        job_id=f"job_{uuid.uuid4().hex[:12]}",
        user_id=user.id,
        ai_config_id=owner_cfg.id,
        ai_kind="core",
        template_id=chosen.get("id") or None,
        title=task_title,
        instruction=chosen.get("instruction") or "",
        task_payload=json.dumps(task_payload, ensure_ascii=False),
        priority=max(1, min(10, int(chosen.get("priority") or 5))),
        status="queued",
        trigger_type="schedule" if schedule_enabled else "manual",
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return {
        "success": True,
        "job_id": row.job_id,
        "title": row.title,
        "priority": row.priority,
        "task_payload": task_payload,
        "owner_ai_config_id": owner_cfg.id,
        "owner_ai_name": owner_cfg.name,
        "requested_ai_config_id": cfg.id,
    }

@router.get("/configs/{config_id}/task-list")
async def get_ai_task_list(
    config_id: int,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    cfg = get_ai_config_or_404(session, config_id, user.id)

    templates = normalize_tasks_from_control(cfg.system_auto_control)
    jobs = session.exec(
        select(AITaskJob).where(
            AITaskJob.user_id == user.id,
            AITaskJob.ai_config_id == config_id,
            AITaskJob.status.in_(["queued", "running", "paused"]),
        ).order_by(AITaskJob.priority.desc(), AITaskJob.created_at.asc())
    ).all()

    running_job = next((job for job in jobs if job.status == "running"), None)
    next_job = next((job for job in jobs if job.status == "queued"), None)
    queued_count_map = {}
    running_count_map = {}
    for job in jobs:
        key = str(job.template_id or "")
        if not key:
            continue
        if job.status == "queued":
            queued_count_map[key] = int(queued_count_map.get(key, 0)) + 1
        if job.status == "running":
            running_count_map[key] = int(running_count_map.get(key, 0)) + 1

    def _task_runtime_state(task_id: str, schedule_enabled: bool) -> str:
        if running_job and str(running_job.template_id or "") == task_id:
            return "running"
        if next_job and str(next_job.template_id or "") == task_id:
            return "next"
        if schedule_enabled:
            return "scheduled"
        return "idle"

    result = []
    for task in templates:
        task_id = str(task.get("id") or "")
        schedule_enabled = bool(task.get("schedule_enabled", False))
        result.append({
            **task,
            "runtime_state": _task_runtime_state(task_id, schedule_enabled),
            "queued_count": int(queued_count_map.get(task_id, 0)),
            "running_count": int(running_count_map.get(task_id, 0)),
        })

    result.sort(
        key=lambda item: (
            -int(item.get("priority") or 0),
            str(item.get("title") or ""),
        )
    )
    return {
        "ai_config_id": config_id,
        "ai_name": cfg.name,
        "tasks": result,
    }

@router.get("/configs/{config_id}/task-jobs")
async def get_ai_task_jobs(
    config_id: int,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    cfg = get_ai_config_or_404(session, config_id, user.id)

    jobs = session.exec(
        select(AITaskJob).where(
            AITaskJob.user_id == user.id,
            AITaskJob.ai_config_id == config_id,
        ).order_by(AITaskJob.created_at.desc())
    ).all()

    runs = session.exec(
        select(ChatRun).where(
            ChatRun.user_id == user.id,
            ChatRun.ai_config_id == config_id,
            ChatRun.ai_kind == "core",
        ).order_by(ChatRun.created_at.asc())
    ).all()
    latest_run_by_prefix: dict[str, ChatRun] = {}
    for run in runs:
        sid = str(run.session_id or "")
        if not sid.startswith("session_task_job_"):
            continue
        prefix = sid.split("_g")[0] if "_g" in sid else sid
        prev = latest_run_by_prefix.get(prefix)
        if prev is None or float(run.updated_at or run.created_at or 0) >= float(prev.updated_at or prev.created_at or 0):
            latest_run_by_prefix[prefix] = run

    msg_rows = session.exec(
        select(ChatMessage).where(
            ChatMessage.user_id == user.id,
            ChatMessage.ai_config_id == config_id,
            ChatMessage.ai_kind == "core",
        )
    ).all()
    task_tokens_by_prefix: dict[str, int] = {}
    for msg in msg_rows:
        sid = str(msg.session_id or "")
        if not sid.startswith("session_task_"):
            continue
        prefix = sid.split("_g")[0] if "_g" in sid else sid
        task_tokens_by_prefix[prefix] = task_tokens_by_prefix.get(prefix, 0) + int(msg.total_tokens or 0)

    try:
        from api.chat_runtime.run_state import _RUN_LIVE_STATE, _RUN_STATE_LOCK  # type: ignore
        with _RUN_STATE_LOCK:
            live_map = dict(_RUN_LIVE_STATE)
    except Exception:
        live_map = {}

    out = []
    for job in jobs:
        prefix = f"session_task_{job.job_id}"
        active_run = find_task_active_run(session, user.id, config_id, job)
        run_status = str(active_run.status) if active_run else ""
        effective_status = str(job.status or "")
        if run_status in {"queued", "running"} and effective_status in {"queued", "running"}:
            effective_status = "running" if run_status == "running" else "queued"
        latest_run = active_run or latest_run_by_prefix.get(prefix)
        live = live_map.get(latest_run.run_id) if latest_run else {}
        live = live if isinstance(live, dict) else {}
        task_payload = decode_task_payload(job.task_payload)
        token_limit = int(cfg.token_limit or 0)
        override_token = task_payload.get("override_token_limit") if isinstance(task_payload, dict) else {}
        if isinstance(override_token, dict) and override_token.get("enabled"):
            try:
                token_limit = max(1, int(override_token.get("value") or token_limit or 1))
            except Exception:
                token_limit = int(cfg.token_limit or 0)
        out.append(
            {
                "job_id": job.job_id,
                "title": job.title,
                "instruction": job.instruction,
                "priority": job.priority,
                "status": job.status,
                "effective_status": effective_status,
                "run_status": run_status,
                "trigger_type": job.trigger_type,
                "task_payload": task_payload,
                "last_run_id": job.last_run_id,
                "session_id": job.session_id,
                "created_at": job.created_at,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "task_token_used": int(task_tokens_by_prefix.get(prefix, 0) or 0),
                "task_token_limit": token_limit,
                "latest_thinking": str(live.get("reasoning") or live.get("text") or ""),
                "live_phase": str(live.get("phase") or "idle"),
                "live_tool": str(live.get("current_tool") or ""),
                "live_updated_at": live.get("updated_at"),
            }
        )
    return {"ai_config_id": config_id, "jobs": out}

@router.patch("/configs/{config_id}/task-jobs/{job_id}")
async def update_ai_task_job(
    config_id: int,
    job_id: str,
    body: dict,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    cfg = get_ai_config_or_404(session, config_id, user.id)

    job = session.exec(
        select(AITaskJob).where(
            AITaskJob.user_id == user.id,
            AITaskJob.ai_config_id == config_id,
            AITaskJob.job_id == job_id,
        )
    ).first()
    if not job:
        raise HTTPException(status_code=404, detail="Task job not found")

    payload_body = body if isinstance(body, dict) else {}
    previous_status = str(job.status or "")
    if "title" in payload_body:
        title = str(payload_body.get("title") or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="title cannot be empty")
        job.title = title
    if "instruction" in payload_body:
        instruction = str(payload_body.get("instruction") or "").strip()
        if not instruction:
            raise HTTPException(status_code=400, detail="instruction cannot be empty")
        job.instruction = instruction
    if "priority" in payload_body:
        try:
            job.priority = max(1, min(10, int(payload_body.get("priority") or 5)))
        except Exception:
            raise HTTPException(status_code=400, detail="priority must be an integer 1-10")
    if "status" in payload_body:
        status = str(payload_body.get("status") or "").strip().lower()
        if status not in {"queued", "paused"}:
            raise HTTPException(status_code=400, detail="status can only be queued or paused")
        if previous_status in {"completed", "cancelled", "stopped", "error"}:
            raise HTTPException(status_code=400, detail="Cannot update a finished task status")
        job.status = status
        if status == "queued":
            job.finished_at = None

    schedule_keys = {
        "schedule_enabled",
        "schedule_loop_enabled",
        "schedule_run_immediately",
        "schedule_duration_minutes",
        "schedule_at",
        "schedule_loop_mode",
        "schedule_daily_time",
        "schedule_weekly_days",
        "schedule_max_runs",
        "schedule_end_at",
        "mode",
    }
    existing_payload = decode_task_payload(job.task_payload)
    if any(key in payload_body for key in schedule_keys):
        schedule_source = dict(payload_body)
        mode = str(schedule_source.get("mode") or "").strip().lower()
        if mode == "immediate":
            schedule_source["schedule_enabled"] = False
            schedule_source["schedule_loop_enabled"] = False
            schedule_source["schedule_run_immediately"] = False
        elif mode == "scheduled":
            schedule_source["schedule_enabled"] = True
            schedule_source["schedule_loop_enabled"] = False
            schedule_source["schedule_run_immediately"] = False
        elif mode == "recurring":
            schedule_source["schedule_enabled"] = True
            schedule_source["schedule_loop_enabled"] = True
        elif mode:
            raise HTTPException(status_code=400, detail="mode must be immediate, scheduled, or recurring")
        # 解析 + schedule_at 补全统一由 extract_task_payload 完成
        patch_payload = extract_task_payload(schedule_source)
        existing_payload["schedule"] = patch_payload.get("schedule", {})
    job.task_payload = json.dumps(existing_payload, ensure_ascii=False)
    schedule = existing_payload.get("schedule") if isinstance(existing_payload, dict) else {}
    job.trigger_type = "schedule" if isinstance(schedule, dict) and bool(schedule.get("enabled")) else "manual"
    job.updated_at = time.time()
    session.add(job)
    session.commit()
    session.refresh(job)
    return {
        "success": True,
        "job_id": job.job_id,
        "previous_status": previous_status,
        "title": job.title,
        "instruction": job.instruction,
        "priority": job.priority,
        "status": job.status,
        "trigger_type": job.trigger_type,
        "task_payload": decode_task_payload(job.task_payload),
    }

@router.post("/configs/{config_id}/task-jobs/{job_id}/stop")
async def stop_ai_task_job(
    config_id: int,
    job_id: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    cfg = get_ai_config_or_404(session, config_id, user.id)

    job = session.exec(
        select(AITaskJob).where(
            AITaskJob.user_id == user.id,
            AITaskJob.ai_config_id == config_id,
            AITaskJob.job_id == job_id,
        )
    ).first()
    if not job:
        raise HTTPException(status_code=404, detail="Task job not found")

    prev_status = str(job.status or "")
    if prev_status in {"completed", "cancelled", "stopped", "error"}:
        return {"success": True, "job_id": job.job_id, "status": prev_status, "already_stopped": True}

    now = time.time()
    run_row = find_task_active_run(session, user.id, config_id, job)
    related_session_ids = iter_task_session_ids(job.job_id, job.session_id)

    for session_prefix in related_session_ids:
        run_rows = session.exec(
            select(ChatRun).where(
                ChatRun.user_id == user.id,
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
                ChatMessage.user_id == user.id,
                ChatMessage.ai_config_id == config_id,
                ChatMessage.ai_kind == "core",
                ChatMessage.session_id.like(f"{session_prefix}%"),
            )
        ).all()
        delete_message_media(session, msg_rows)
        for msg in msg_rows:
            session.delete(msg)

        session_rows = session.exec(
            select(ChatSession).where(
                ChatSession.user_id == user.id,
                ChatSession.ai_config_id == config_id,
                ChatSession.ai_kind == "core",
                ChatSession.session_id.like(f"{session_prefix}%"),
            )
        ).all()
        for row in session_rows:
            session.delete(row)

    job.status = "stopped"
    job.finished_at = now
    job.updated_at = now
    session.add(job)
    session.commit()
    session.refresh(job)
    return {
        "success": True,
        "job_id": job.job_id,
        "status": job.status,
        "previous_status": prev_status,
        "run_id": run_row.run_id if run_row else None,
    }

@router.post("/configs/{config_id}/task-jobs/{job_id}/pause")
async def pause_ai_task_job(
    config_id: int,
    job_id: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    cfg = get_ai_config_or_404(session, config_id, user.id)
    job = session.exec(
        select(AITaskJob).where(
            AITaskJob.user_id == user.id,
            AITaskJob.ai_config_id == config_id,
            AITaskJob.job_id == job_id,
        )
    ).first()
    if not job:
        raise HTTPException(status_code=404, detail="Task job not found")
    if str(job.status or "") in {"completed", "cancelled", "stopped", "error"}:
        raise HTTPException(status_code=400, detail="Task already finished")

    run_row = find_task_active_run(session, user.id, config_id, job)
    if run_row:
        run_row.stop_requested = True
        run_row.updated_at = time.time()
        session.add(run_row)
    job.status = "paused"
    job.updated_at = time.time()
    session.add(job)
    session.commit()
    return {"success": True, "job_id": job.job_id, "status": job.status, "run_id": run_row.run_id if run_row else None}

@router.post("/configs/{config_id}/task-jobs/{job_id}/resume")
async def resume_ai_task_job(
    config_id: int,
    job_id: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    cfg = get_ai_config_or_404(session, config_id, user.id)
    job = session.exec(
        select(AITaskJob).where(
            AITaskJob.user_id == user.id,
            AITaskJob.ai_config_id == config_id,
            AITaskJob.job_id == job_id,
        )
    ).first()
    if not job:
        raise HTTPException(status_code=404, detail="Task job not found")
    if str(job.status or "") in {"completed", "cancelled", "stopped", "error"}:
        raise HTTPException(status_code=400, detail="Task already finished")

    active_run = find_task_active_run(session, user.id, config_id, job)
    if active_run:
        return {"success": True, "job_id": job.job_id, "status": "running", "run_id": active_run.run_id}

    job.status = "queued"
    job.finished_at = None
    job.updated_at = time.time()
    session.add(job)
    session.commit()
    return {"success": True, "job_id": job.job_id, "status": job.status}

@router.delete("/configs/{config_id}/task-jobs/{job_id}")
async def delete_ai_task_job(
    config_id: int,
    job_id: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    cfg = get_ai_config_or_404(session, config_id, user.id)
    job = session.exec(
        select(AITaskJob).where(
            AITaskJob.user_id == user.id,
            AITaskJob.ai_config_id == config_id,
            AITaskJob.job_id == job_id,
        )
    ).first()
    if not job:
        raise HTTPException(status_code=404, detail="Task job not found")

    now = time.time()
    related_session_ids = iter_task_session_ids(job.job_id, job.session_id)

    for session_prefix in related_session_ids:
        run_rows = session.exec(
            select(ChatRun).where(
                ChatRun.user_id == user.id,
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
                ChatMessage.user_id == user.id,
                ChatMessage.ai_config_id == config_id,
                ChatMessage.ai_kind == "core",
                ChatMessage.session_id.like(f"{session_prefix}%"),
            )
        ).all()
        delete_message_media(session, msg_rows)
        for msg in msg_rows:
            session.delete(msg)

        session_rows = session.exec(
            select(ChatSession).where(
                ChatSession.user_id == user.id,
                ChatSession.ai_config_id == config_id,
                ChatSession.ai_kind == "core",
                ChatSession.session_id.like(f"{session_prefix}%"),
            )
        ).all()
        for row in session_rows:
            session.delete(row)

    # Hard delete task record itself so deletion is visible immediately in UI list.
    session.delete(job)
    session.commit()
    return {"success": True, "job_id": job_id, "deleted": True}
