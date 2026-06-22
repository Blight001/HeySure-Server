"""Automated task scheduler: start and supervise task-driven chat runs, and
periodically dispatch due scheduled jobs from each config's
``system_auto_control``."""

IS_ROUTER_ENTRY = False

import json
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from api.database import engine
from api.models import AITaskJob, AssistantAIConfig, ChatMessageCreate, ChatRun, ChatSession, User
from api.services import librarian_service
from api.services.task_system import DEFAULT_SYSTEM_AUTO_CONTROL, normalize_system_auto_control
from .run_state import MAX_AUTO_SUPERVISION_ROUNDS, _RUN_THREADS
from api.services.chat_persistence import _save_message
import logging


logger = logging.getLogger(__name__)


def _start_task_run(
    session: Session,
    cfg: AssistantAIConfig,
    job: AITaskJob,
    task_prompt: str,
    trigger_type: str,
) -> Optional[str]:
    session_prefix = f"session_task_{job.job_id}"
    # One conversation per task. The 传宗接代/代际 lineage (per-run
    # ``_g{n}`` generation sessions) was removed: every run of the job — first
    # dispatch, supervision, or loop — reuses this single task session.
    session_id = session_prefix
    job.session_id = session_id
    sname = f"任务: {job.title}"
    chat_session = session.exec(
        select(ChatSession).where(
            ChatSession.user_id == cfg.user_id,
            ChatSession.ai_config_id == cfg.id,
            ChatSession.ai_kind == "core",
            ChatSession.session_id == session_id,
        )
    ).first()
    if not chat_session:
        chat_session = ChatSession(
            user_id=cfg.user_id,
            ai_config_id=cfg.id,
            ai_kind="core",
            session_id=session_id,
            session_name=sname,
        )
        session.add(chat_session)
        session.commit()

    payload = {}
    try:
        payload = json.loads(job.task_payload) if job.task_payload else {}
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    payload_lines: List[str] = []
    schedule = payload.get("schedule") if isinstance(payload, dict) else {}
    if isinstance(schedule, dict) and schedule.get("enabled"):
        duration = int(schedule.get("duration_minutes") or 0)
        schedule_at = float(schedule.get("schedule_at") or 0)
        if duration > 0:
            payload_lines.append(f"- 定时时长: {duration} 分钟")
        if schedule_at > 0:
            payload_lines.append(f"- 定时日期: {datetime.fromtimestamp(schedule_at).isoformat(sep=' ', timespec='minutes')}")
    token_override = payload.get("override_token_limit") if isinstance(payload, dict) else {}
    if isinstance(token_override, dict) and token_override.get("enabled"):
        payload_lines.append(f"- Token范围覆盖: {int(token_override.get('value') or 0)}")
    mcp_override = payload.get("override_mcp_tools") if isinstance(payload, dict) else {}
    if isinstance(mcp_override, dict) and mcp_override.get("enabled"):
        tools = mcp_override.get("tools")
        if isinstance(tools, list):
            payload_lines.append(f"- MCP范围覆盖: {', '.join(str(t) for t in tools if str(t).strip()) or '（空）'}")
    payload_block = ""
    if payload_lines:
        payload_block = "[任务附加约束]\n" + "\n".join(payload_lines) + "\n\n"

    # 任务派发前的图书管理员预先简报（命中知识库 active 条目则注入）
    briefing_block = ""
    try:
        brief_text = librarian_service.brief(
            user_id=cfg.user_id,
            ai_config_id=cfg.id,
            task_title=str(job.title or ""),
            task_instruction=str(job.instruction or ""),
        )
        if brief_text.strip():
            briefing_block = (
                "[图书管理员预先简报]\n"
                "以下条目可能与本任务相关，建议优先参考其步骤；完整内容可在控制台知识库查看。\n"
                f"{brief_text}\n\n"
            )
    except Exception as _bex:
        logger.exception(f"librarian.brief failed: {_bex}")

    _TRIGGER_LABEL = {
        "schedule": "定时调度",
        "immediate": "立即执行",
        "manual": "手动触发",
        "preempt": "高优先级抢占",
        "supervision": "系统监督续跑",
        "loop": "循环任务",
    }
    trigger_label = _TRIGGER_LABEL.get(str(trigger_type or ""), str(trigger_type or "未知"))
    content = (
        f"[系统提示]\n{task_prompt}\n\n"
        f"[任务系统下发]\n"
        f"- 任务ID: {job.job_id}\n"
        f"- 标题: {job.title}\n"
        f"- 优先级: P{job.priority}\n"
        f"- 触发方式: {trigger_label}\n"
        f"- 要求: {job.instruction}\n\n"
        + payload_block
        + briefing_block
        + "**执行前必做**：先调用 knowledge.search（用任务标题或核心动作构造 query），语义召回图书馆里相关的主题思想（传承思想），包括可复用的操作 SOP、SKILL 包和过往经验。检索结果是决定执行策略和是否制定分阶段计划的依据。\n\n"
        + "简单任务执行结束后自然完成；若使用了 plan.create，则最后必须调用 plan.finish 收尾。"
    )
    user_msg = _save_message(
        session,
        cfg.user_id,
        ChatMessageCreate(
            role="user",
            content=content,
            tags=f"task_dispatch:{job.job_id}",
            ai_config_id=cfg.id,
            ai_kind="core",
            session_id=session_id,
            session_name=sname,
        ),
    )
    run_id = f"run_{uuid.uuid4().hex}"
    worker_extras = {
        "model_user_content": user_msg.content,
        "merged_system_prompt": None,
        "max_steps": None,
    }
    row = ChatRun(
        run_id=run_id,
        user_id=cfg.user_id,
        ai_config_id=cfg.id,
        ai_kind="core",
        session_id=session_id,
        session_name=sname,
        status="queued",
        stop_requested=False,
        worker_kwargs_json=json.dumps(worker_extras, ensure_ascii=False),
    )
    session.add(row)
    job.status = "running"
    job.trigger_type = trigger_type
    job.last_run_id = run_id
    job.started_at = job.started_at or time.time()
    job.updated_at = time.time()
    session.add(job)
    session.commit()
    try:
        from api.services.world_events import emit_world_event
        emit_world_event(cfg.user_id, "task_started", {
            "ai_config_id": cfg.id,
            "job_id": job.job_id,
            "title": str(job.title or ""),
        })
    except Exception:
        logger.exception("emit task_started world event failed")
    from api.core.settings import settings
    from ai_runtime.worker import notify_queue

    if settings.ai_dispatch_mode == "remote":
        notify_queue(run_id)
        return run_id

    from ai_runtime.inference.core import _run_worker

    worker = threading.Thread(
        target=_run_worker,
        kwargs={
            "run_id": run_id,
            "user_id": cfg.user_id,
            "ai_config_id": cfg.id,
            "ai_kind": "core",
            "session_id": session_id,
            "session_name": sname,
            "model_user_content": user_msg.content,
            "merged_system_prompt": None,
            "max_steps": None,
        },
        daemon=True,
    )
    worker.start()
    _RUN_THREADS[run_id] = worker
    return run_id

def _ensure_scheduled_jobs(session: Session, cfg: AssistantAIConfig, ctl: Dict[str, Any], now: float) -> int:
    created = 0
    for task in ctl.get("tasks", []):
        if not (task.get("enabled") and task.get("schedule_enabled")):
            continue
        interval = max(1, int(task.get("interval_minutes") or 30)) * 60
        last = session.exec(
            select(AITaskJob).where(
                AITaskJob.user_id == cfg.user_id,
                AITaskJob.ai_config_id == cfg.id,
                AITaskJob.template_id == task.get("id"),
            ).order_by(AITaskJob.created_at.desc())
        ).first()
        if last and (now - float(last.created_at or 0)) < interval:
            continue
        job = AITaskJob(
            job_id=f"job_{uuid.uuid4().hex[:12]}",
            user_id=cfg.user_id,
            ai_config_id=cfg.id,
            ai_kind="core",
            template_id=str(task.get("id") or ""),
            title=str(task.get("title") or "未命名任务"),
            instruction=str(task.get("instruction") or ""),
            priority=max(1, min(10, int(task.get("priority") or 5))),
            status="queued",
            trigger_type="schedule",
        )
        session.add(job)
        created += 1
    if created:
        session.commit()
    return created

def process_task_scheduler() -> Dict[str, int]:
    now = time.time()
    started = 0
    queued = 0
    supervised = 0
    with Session(engine) as session:
        supervision_idle_seconds_cache: Dict[int, int] = {}

        def _get_supervision_idle_seconds(user_id: int) -> int:
            cached = supervision_idle_seconds_cache.get(user_id)
            if cached is not None:
                return cached
            value = 25
            user_row = session.get(User, user_id)
            if user_row:
                try:
                    value = int(getattr(user_row, "default_supervision_idle_seconds", 25) or 25)
                except Exception:
                    value = 25
            value = max(5, min(3600, value))
            supervision_idle_seconds_cache[user_id] = value
            return value

        cfgs = session.exec(
            select(AssistantAIConfig).where(
                AssistantAIConfig.ai_role == "digital_member",
                AssistantAIConfig.enabled == True,
            )
        ).all()
        from api.services import kb_store

        for cfg in cfgs:
            # 方案 A：自动控制（含定时任务配置）直接读 personas 文件（缺失回退 DB）。
            ctl = normalize_system_auto_control(kb_store.effective_auto_control_json(cfg.user_id, cfg))
            if ctl.get("enabled"):
                queued += _ensure_scheduled_jobs(session, cfg, ctl, now)

            jobs = session.exec(
                select(AITaskJob).where(
                    AITaskJob.user_id == cfg.user_id,
                    AITaskJob.ai_config_id == cfg.id,
                    AITaskJob.status.in_(["queued", "running", "paused"]),
                ).order_by(AITaskJob.priority.desc(), AITaskJob.created_at.asc())
            ).all()

            def _is_job_time_ready(row: AITaskJob) -> bool:
                from api.services.task_schedule import is_time_ready

                try:
                    payload = json.loads(row.task_payload) if row.task_payload else {}
                except Exception:
                    payload = {}
                if not isinstance(payload, dict):
                    return True
                return is_time_ready(payload.get("schedule"), created_at=row.created_at, now=now)

            running = next((j for j in jobs if j.status == "running"), None)
            queued_jobs = [j for j in jobs if j.status == "queued" and _is_job_time_ready(j)]

            active_run = session.exec(
                select(ChatRun).where(
                    ChatRun.user_id == cfg.user_id,
                    ChatRun.ai_config_id == cfg.id,
                    ChatRun.ai_kind == "core",
                    ChatRun.status.in_(["queued", "running"]),
                ).order_by(ChatRun.updated_at.desc())
            ).first()

            if running and not active_run:
                last_run_status = ""
                last_run = None
                if running.last_run_id:
                    last_run = session.exec(
                        select(ChatRun).where(
                            ChatRun.user_id == cfg.user_id,
                            ChatRun.run_id == running.last_run_id,
                        )
                    ).first()
                    if last_run:
                        last_run_status = str(last_run.status or "")

                # If latest run failed/stopped, pause and wait for manual resume.
                if last_run_status in {"error", "stopped"}:
                    running.status = "paused"
                    running.updated_at = now
                    session.add(running)
                    session.commit()
                    continue

                # Prevent auto-supervision from generating endless new generations.
                if int(running.supervision_count or 0) >= MAX_AUTO_SUPERVISION_ROUNDS:
                    running.status = "paused"
                    running.updated_at = now
                    session.add(running)
                    session.commit()
                    continue

                last_sv = float(running.last_supervised_at or 0)
                supervision_idle_seconds = _get_supervision_idle_seconds(cfg.user_id)
                idle_since = last_sv if last_sv > 0 else 0.0
                if idle_since <= 0 and last_run is not None:
                    idle_since = float(last_run.finished_at or last_run.updated_at or 0)
                if idle_since <= 0:
                    idle_since = float(running.updated_at or running.created_at or now)

                if now - idle_since >= supervision_idle_seconds:
                    prompt = str(ctl.get("supervision_prompt") or DEFAULT_SYSTEM_AUTO_CONTROL["supervision_prompt"])
                    if _start_task_run(session, cfg, running, prompt, "supervision"):
                        running.last_supervised_at = now
                        running.supervision_count = int(running.supervision_count or 0) + 1
                        session.add(running)
                        session.commit()
                        supervised += 1
                continue

            if running and queued_jobs:
                top = queued_jobs[0]
                if int(top.priority or 0) > int(running.priority or 0):
                    if active_run and active_run.session_id == running.session_id:
                        active_run.stop_requested = True
                        active_run.updated_at = now
                        session.add(active_run)
                    running.status = "paused"
                    running.updated_at = now
                    session.add(running)
                    session.commit()
                    prompt = str(ctl.get("start_task_prompt") or DEFAULT_SYSTEM_AUTO_CONTROL["start_task_prompt"])
                    if _start_task_run(session, cfg, top, prompt, "preempt"):
                        started += 1
                continue

            if running:
                continue

            if queued_jobs:
                prompt = str(ctl.get("start_task_prompt") or DEFAULT_SYSTEM_AUTO_CONTROL["start_task_prompt"])
                if _start_task_run(session, cfg, queued_jobs[0], prompt, queued_jobs[0].trigger_type or "schedule"):
                    started += 1
                continue

            # Paused jobs should be resumed manually by explicit user action.
            # Do not auto-resume here, otherwise "pause" becomes ineffective.
    return {"started": started, "queued": queued, "supervised": supervised}
