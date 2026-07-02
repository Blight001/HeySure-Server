"""``/api/ai`` miscellaneous routes: clear/delete configs, runtime status and bot
statuses, token usage snapshots, member cards, and session creation."""

IS_ROUTER_ENTRY = False

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Depends, Header
from sqlmodel import Session, select

from connector_runtime.bots import iter_bots
from api.core.config import CONNECTOR_RUNTIME_URL
from api.database import get_session
from api.runtime.internal_http import InternalClient
from api.models import (
    DeviceAiBinding,
    DeviceTypeMcpPermission,
    AITaskJob,
    AIRuntimeStatus,
    AssistantAIConfig,
    ChatMessage,
    ChatRun,
    ChatSession,
    DevicePresence,
    ChatSessionCreate,
    TokenUsageSnapshot,
)
from api.services.access.access_guards import get_ai_config_or_404
from api.sio import agents
from api.devices.live import emit_agent_list_for_user
from .auth import get_current_user
from ai_runtime.inference.ai_service import ensure_default_ai_for_user
from api.services.model_presets import resolve_model_preset
from api.services.tasks.task_system import decode_task_payload
from .ai_base import router


def _empty_bot_statuses() -> Dict[str, Dict[int, Dict[str, str]]]:
    """``{channel: {ai_config_id: state}}`` zero-initialized for every bot."""
    return {bot.channel: {} for bot in iter_bots()}


def _load_connector_runtime_bot_statuses() -> Tuple[Dict[str, Dict[int, Dict[str, str]]], Optional[str]]:
    """Fetch per-bot state from connector-runtime; fall back gracefully.

    The connector replies with one ``<channel>_statuses`` field per bot
    (``feishu_statuses``, ``qq_statuses``, …). Any registered bot whose
    field is absent comes back with an empty dict so callers can iterate
    every channel uniformly.
    """
    if not CONNECTOR_RUNTIME_URL:
        return _empty_bot_statuses(), None
    # Bot status is dashboard decoration, not a critical save-path dependency.
    # Keep this short so a stopped connector-runtime does not make config saves
    # feel stuck when the frontend refreshes cards after saving.
    client = InternalClient(CONNECTOR_RUNTIME_URL, timeout=1.5)
    try:
        payload = client.get("/internal/bot/statuses")
    except Exception as exc:
        return _empty_bot_statuses(), str(exc)
    finally:
        client.close()

    statuses = _empty_bot_statuses()
    if isinstance(payload, dict):
        for bot in iter_bots():
            field = f"{bot.channel}_statuses"
            for raw_id, state in (payload.get(field) or {}).items():
                try:
                    config_id = int(raw_id)
                except Exception:
                    continue
                if isinstance(state, dict):
                    statuses[bot.channel][config_id] = {
                        "status": str(state.get("status") or "failed"),
                        "mode": str(state.get("mode") or "long_connection"),
                        "label": str(state.get("label") or ""),
                        "message": str(state.get("message") or ""),
                    }
    return statuses, None


@router.post("/configs/{config_id}/clear-tokens")
def clear_ai_token_usage(
    config_id: int,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    cfg = get_ai_config_or_404(session, config_id, user.id)

    rows = session.exec(
        select(TokenUsageSnapshot).where(
            TokenUsageSnapshot.user_id == user.id,
            TokenUsageSnapshot.ai_kind == "assistant",
            TokenUsageSnapshot.ai_config_id == config_id,
        )
    ).all()
    deleted = len(rows)
    for row in rows:
        session.delete(row)
    session.commit()
    return {"success": True, "deleted": deleted}

@router.delete("/configs/{config_id}")
async def delete_ai_config(
    config_id: int,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    cfg = get_ai_config_or_404(session, config_id, user.id)

    status_rows = session.exec(
        select(AIRuntimeStatus).where(
            AIRuntimeStatus.user_id == user.id,
            AIRuntimeStatus.ai_config_id == config_id,
            AIRuntimeStatus.ai_kind == "assistant",
        )
    ).all()
    for row in status_rows:
        session.delete(row)

    session_rows = session.exec(
        select(ChatSession).where(
            ChatSession.user_id == user.id,
            ChatSession.ai_config_id == config_id,
            ChatSession.ai_kind == "assistant",
        )
    ).all()
    for row in session_rows:
        session.delete(row)

    binding_rows = session.exec(
        select(DeviceAiBinding).where(
            DeviceAiBinding.user_id == user.id,
            DeviceAiBinding.ai_config_id == config_id,
        )
    ).all()
    for row in binding_rows:
        session.delete(row)

    presence_rows = session.exec(
        select(DevicePresence).where(
            DevicePresence.user_id == user.id,
            DevicePresence.ai_config_id == config_id,
        )
    ).all()
    for row in presence_rows:
        row.ai_config_id = None
        row.updated_at = time.time()
        session.add(row)

    scope_rows = session.exec(
        select(DeviceTypeMcpPermission).where(
            DeviceTypeMcpPermission.user_id == user.id,
            DeviceTypeMcpPermission.ai_config_id == config_id,
        )
    ).all()
    for row in scope_rows:
        row.ai_config_id = None
        row.updated_at = time.time()
        session.add(row)

    session.delete(cfg)
    session.commit()

    changed_live = False
    for agent in agents.values():
        if agent.get("userId") != user.id:
            continue
        try:
            bound_id = int(agent.get("aiConfigId") or agent.get("ai_config_id") or 0)
        except (TypeError, ValueError):
            bound_id = 0
        if bound_id == config_id:
            agent["aiConfigId"] = None
            changed_live = True
    if changed_live:
        await emit_agent_list_for_user(user.id)
    return {"success": True}

@router.get("/runtime-status")
def get_runtime_status(
    ai_config_id: Optional[int] = None,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    statement = select(AIRuntimeStatus).where(AIRuntimeStatus.user_id == user.id)
    if ai_config_id:
        statement = statement.where(AIRuntimeStatus.ai_config_id == ai_config_id)
    return session.exec(statement).all()

@router.get("/token-snapshots")
def get_token_snapshots(
    ai_kind: str = "assistant",
    ai_config_id: Optional[int] = None,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    statement = select(TokenUsageSnapshot).where(
        TokenUsageSnapshot.user_id == user.id,
        TokenUsageSnapshot.ai_kind == ai_kind,
    )
    if ai_config_id:
        statement = statement.where(TokenUsageSnapshot.ai_config_id == ai_config_id)
    rows = session.exec(statement).all()
    return rows

@router.get("/cards")
async def list_ai_cards(
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    ensure_default_ai_for_user(session, user.id)

    cfgs = session.exec(
        select(AssistantAIConfig)
        .where(AssistantAIConfig.user_id == user.id)
        .order_by(AssistantAIConfig.sort_order.asc(), AssistantAIConfig.created_at.asc())
    ).all()
    if _ensure_card_configs_enabled(session, user.id, cfgs):
        cfgs = session.exec(
            select(AssistantAIConfig)
            .where(AssistantAIConfig.user_id == user.id)
            .order_by(AssistantAIConfig.sort_order.asc(), AssistantAIConfig.created_at.asc())
        ).all()
    statuses = session.exec(
        select(AIRuntimeStatus).where(
            AIRuntimeStatus.user_id == user.id,
            AIRuntimeStatus.ai_kind == "assistant",
        )
    ).all()
    snapshots = session.exec(
        select(TokenUsageSnapshot).where(
            TokenUsageSnapshot.user_id == user.id,
            TokenUsageSnapshot.ai_kind == "assistant",
        )
    ).all()

    status_map = {row.ai_config_id: row for row in statuses if row.ai_config_id is not None}
    token_totals = {}
    for row in snapshots:
        if row.ai_config_id is None:
            continue
        token_totals[row.ai_config_id] = token_totals.get(row.ai_config_id, 0) + (row.total_tokens or 0)

    core_sessions = session.exec(
        select(ChatSession).where(
            ChatSession.user_id == user.id,
            ChatSession.ai_kind == "core",
        )
    ).all()
    latest_core_session_by_cfg: Dict[int, ChatSession] = {}
    for row in core_sessions:
        if row.ai_config_id is None:
            continue
        key = int(row.ai_config_id)
        prev = latest_core_session_by_cfg.get(key)
        if not prev or (row.updated_at or 0) > (prev.updated_at or 0):
            latest_core_session_by_cfg[key] = row

    core_messages = session.exec(
        select(ChatMessage).where(
            ChatMessage.user_id == user.id,
            ChatMessage.ai_kind == "core",
        )
    ).all()
    core_session_tokens: Dict[tuple[Optional[int], str], int] = {}
    latest_task_assistant_by_session: Dict[tuple[int, str], ChatMessage] = {}
    for msg in core_messages:
        key = (msg.ai_config_id, msg.session_id)
        core_session_tokens[key] = core_session_tokens.get(key, 0) + int(msg.total_tokens or 0)
        session_id = str(msg.session_id or "")
        if (
            msg.ai_config_id is not None
            and msg.role == "assistant"
            and session_id.startswith("session_task_")
        ):
            message_key = (int(msg.ai_config_id), session_id)
            previous = latest_task_assistant_by_session.get(message_key)
            if previous is None or float(msg.created_at or 0) > float(previous.created_at or 0):
                latest_task_assistant_by_session[message_key] = msg
    core_task_tokens_by_prefix: Dict[tuple[int, str], int] = {}
    for (cfg_id, sid), total in core_session_tokens.items():
        if cfg_id is None:
            continue
        session_id = str(sid or "")
        if not session_id.startswith("session_task_"):
            continue
        session_prefix = session_id.split("_g")[0] if "_g" in session_id else session_id
        token_key = (int(cfg_id), session_prefix)
        core_task_tokens_by_prefix[token_key] = core_task_tokens_by_prefix.get(token_key, 0) + int(total or 0)

    active_runs = session.exec(
        select(ChatRun).where(
            ChatRun.user_id == user.id,
            ChatRun.status.in_(["queued", "running"]),
        ).order_by(ChatRun.updated_at.desc())
    ).all()
    active_run_map = {}
    for row in active_runs:
        key = (row.ai_config_id, row.ai_kind)
        if key not in active_run_map:
            active_run_map[key] = row

    recent_user_chat_cutoff = time.time() - 60
    recent_user_messages = session.exec(
        select(ChatMessage).where(
            ChatMessage.user_id == user.id,
            ChatMessage.role == "user",
            ChatMessage.created_at >= recent_user_chat_cutoff,
        ).order_by(ChatMessage.created_at.desc())
    ).all()
    recent_user_chat_map: Dict[tuple[Optional[int], str], ChatMessage] = {}
    for row in recent_user_messages:
        session_id = str(row.session_id or "")
        if session_id.startswith("session_task_"):
            continue
        key = (row.ai_config_id, row.ai_kind)
        if key not in recent_user_chat_map:
            recent_user_chat_map[key] = row

    task_jobs = session.exec(
        select(AITaskJob).where(
            AITaskJob.user_id == user.id,
        ).order_by(AITaskJob.created_at.desc())
    ).all()
    task_jobs_by_cfg: Dict[int, List[AITaskJob]] = {}
    for row in task_jobs:
        task_jobs_by_cfg.setdefault(int(row.ai_config_id), []).append(row)

    task_runs = session.exec(
        select(ChatRun).where(
            ChatRun.user_id == user.id,
            ChatRun.ai_kind == "core",
            ChatRun.session_id.like("session_task_%"),
        ).order_by(ChatRun.updated_at.desc())
    ).all()
    task_active_status_by_prefix: Dict[str, str] = {}
    for row in task_runs:
        sid = str(row.session_id or "").strip()
        if not sid.startswith("session_task_"):
            continue
        session_prefix = sid.split("_g")[0] if "_g" in sid else sid
        if session_prefix not in task_active_status_by_prefix and str(row.status or "") in {"queued", "running"}:
            task_active_status_by_prefix[session_prefix] = str(row.status or "")

    try:
        from api.chat_runtime.run_state import _RUN_LIVE_STATE, _RUN_STATE_LOCK  # type: ignore
        with _RUN_STATE_LOCK:
            run_live_state = dict(_RUN_LIVE_STATE)
    except Exception:
        run_live_state = {}

    def _build_task_summary(job: AITaskJob, token_limit: int) -> Dict[str, Any]:
        session_prefix = f"session_task_{job.job_id}"
        run_status = str(task_active_status_by_prefix.get(session_prefix) or "")
        effective_status = str(job.status or "")
        if run_status in {"queued", "running"} and effective_status in {"queued", "running"}:
            effective_status = "running" if run_status == "running" else "queued"
        task_token_used = core_task_tokens_by_prefix.get((int(job.ai_config_id), session_prefix), 0)
        task_payload = decode_task_payload(job.task_payload)
        schedule = task_payload.get("schedule") if isinstance(task_payload, dict) else {}
        schedule = schedule if isinstance(schedule, dict) else {}
        try:
            schedule_at = float(schedule.get("schedule_at") or 0)
        except Exception:
            schedule_at = 0.0
        try:
            schedule_duration_minutes = int(schedule.get("duration_minutes") or 0)
        except Exception:
            schedule_duration_minutes = 0
        return {
            "job_id": job.job_id,
            "title": job.title,
            "status": job.status,
            "effective_status": effective_status,
            "run_status": run_status,
            "trigger_type": job.trigger_type,
            "schedule_enabled": bool(schedule.get("enabled")),
            "schedule_at": schedule_at if schedule_at > 0 else None,
            "schedule_loop_enabled": bool(schedule.get("loop_enabled")),
            "schedule_duration_minutes": schedule_duration_minutes,
            "task_token_used": int(task_token_used or 0),
            "task_token_limit": int(token_limit or 0),
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
        }

    def _task_activity_ts(item: Dict[str, Any]) -> float:
        for key in ("finished_at", "updated_at", "started_at", "created_at"):
            try:
                value = float(item.get(key) or 0)
            except Exception:
                value = 0.0
            if value > 0:
                return value
        return 0.0

    def _is_scheduled_task(item: Dict[str, Any]) -> bool:
        return (
            str(item.get("trigger_type") or "").lower() == "schedule"
            or bool(item.get("schedule_enabled"))
        )

    def _is_unfinished_task(item: Dict[str, Any]) -> bool:
        status = str(item.get("effective_status") or item.get("status") or "").lower()
        return status not in {"completed", "done", "finished", "cancelled", "stopped", "error"}

    def _scheduled_task_sort_key(item: Dict[str, Any]) -> tuple[float, float]:
        try:
            schedule_at = float(item.get("schedule_at") or 0)
        except Exception:
            schedule_at = 0.0
        return (schedule_at if schedule_at > 0 else float("inf"), -_task_activity_ts(item))

    remote_bot_statuses, remote_status_error = await asyncio.to_thread(_load_connector_runtime_bot_statuses)
    # Only surface the connector-runtime error once we actually expect a
    # remote answer; in monolith mode there's no remote and the local
    # state is authoritative.
    effective_remote_error = remote_status_error if CONNECTOR_RUNTIME_URL else None

    def _bot_status(cfg: AssistantAIConfig, channel: str) -> Dict[str, str]:
        bot = next((b for b in iter_bots() if b.channel == channel), None)
        if bot is None:
            return {"status": "disabled", "mode": "off", "label": "未启用", "message": "未知机器人"}
        return bot.build_status(
            cfg,
            remote_state=remote_bot_statuses.get(channel, {}).get(int(cfg.id or 0)),
            remote_error=effective_remote_error,
        )

    cards = []
    for cfg in cfgs:
        status = status_map.get(cfg.id)
        token_used = token_totals.get(cfg.id, 0)
        if cfg.ai_role == "digital_member":
            latest_session = latest_core_session_by_cfg.get(int(cfg.id))
            if latest_session:
                token_used = core_session_tokens.get((cfg.id, latest_session.session_id), 0)
            else:
                token_used = 0
        ai_kind = "assistant" if cfg.ai_role == "assistant_admin" else "core"
        active_run = active_run_map.get((cfg.id, ai_kind))
        recent_user_chat = recent_user_chat_map.get((cfg.id, ai_kind))
        run_live = run_live_state.get(active_run.run_id, {}) if active_run else {}
        active_run_session_id = str(active_run.session_id or "") if active_run else ""
        is_task_run_active = active_run_session_id.startswith("session_task_")
        user_chat_active = bool(active_run and not is_task_run_active)
        recent_user_chat_active = bool(recent_user_chat)
        live_token_pending = int(run_live.get("pending_total_tokens") or 0)
        token_used = int(token_used or 0) + live_token_pending
        live_text = str(run_live.get("text") or "")
        live_reasoning = str(run_live.get("reasoning") or "")
        live_tool = str(run_live.get("current_tool") or "").strip()
        latest_task_message = latest_task_assistant_by_session.get((int(cfg.id), active_run_session_id))
        persisted_task_thinking = ""
        if latest_task_message is not None:
            persisted_task_thinking = str(
                latest_task_message.think or latest_task_message.content or ""
            ).strip()
        cfg_task_jobs = task_jobs_by_cfg.get(int(cfg.id), [])
        cfg_task_summaries = [_build_task_summary(job, int(cfg.token_limit or 0)) for job in cfg_task_jobs]
        cfg_task_summaries_by_activity = sorted(
            cfg_task_summaries,
            key=_task_activity_ts,
            reverse=True,
        )
        current_task = next(
            (
                item for item in cfg_task_summaries_by_activity
                if str(item.get("effective_status") or "").lower() == "running"
            ),
            None,
        )
        if current_task is None:
            current_task = next(
                (
                    item for item in cfg_task_summaries_by_activity
                    if str(item.get("effective_status") or "").lower() in {"queued", "paused"}
                    and not _is_scheduled_task(item)
                ),
                None,
            )
        latest_completed_task = next(
            (
                item for item in cfg_task_summaries_by_activity
                if str(item.get("effective_status") or "").lower() in {"completed", "done", "finished"}
            ),
            None,
        )
        scheduled_tasks = [
            item for item in cfg_task_summaries_by_activity
            if _is_scheduled_task(item)
            and _is_unfinished_task(item)
            and (not current_task or item.get("job_id") != current_task.get("job_id"))
        ]
        scheduled_tasks = sorted(scheduled_tasks, key=_scheduled_task_sort_key)[:3]
        current_or_recent_task = current_task or latest_completed_task
        current_task_title = str(current_task.get("title") or "") if current_task else ""
        current_task_status = str(current_task.get("effective_status") or "idle") if current_task else "idle"
        feishu_status = _bot_status(cfg, "feishu")
        qq_status = _bot_status(cfg, "qq")
        bot_channel = str(cfg.bot_channel or "feishu")
        # Surface every registered bot's parsed config slice so the
        # frontend reads ``card.bot_configs.<channel>.<field>`` instead of
        # the legacy flat columns.
        bot_configs_view = {bot.channel: bot.read_config(cfg) for bot in iter_bots()}
        active_bot = next((b for b in iter_bots() if b.channel == bot_channel), None)
        active_bot_enabled = bool(active_bot.read_config(cfg).get("enabled")) if active_bot else False
        _, _, effective_model = resolve_model_preset(user, cfg)
        cards.append(
            {
                "id": cfg.id,
                "name": cfg.name,
                "description": cfg.description,
                "model": effective_model or cfg.model,
                "model_preset_id": cfg.model_preset_id,
                "strip_markdown_symbols": cfg.strip_markdown_symbols,
                "ai_role": cfg.ai_role,
                "digital_member_role": cfg.digital_member_role,
                "is_librarian": cfg.is_librarian,
                "platform": cfg.platform,
                "generation": cfg.generation,
                "token_limit": cfg.token_limit,
                "token_used": token_used,
                "token_live_pending": live_token_pending,
                "lifecycle_status": cfg.lifecycle_status,
                "current_behavior": cfg.current_behavior,
                "workspace_root": cfg.workspace_root,
                "database_uri": cfg.database_uri,
                "project_id": cfg.project_id,
                "project_name": cfg.project_name,
                "parent_ai_config_id": cfg.parent_ai_config_id,
                "root_manager_ai_config_id": cfg.root_manager_ai_config_id,
                "management_scope": cfg.management_scope,
                "enabled": True,
                "mcp_enabled": cfg.mcp_enabled,
                "bot_channel": bot_channel,
                # Per-channel config slices (replaces the flat feishu_*/qq_* columns).
                "bot_configs": bot_configs_view,
                # Per-channel runtime status (one entry per registered bot).
                "bot_statuses": {"feishu": feishu_status, "qq": qq_status},
                "bot_enabled": active_bot_enabled,
                "bot_status": qq_status if bot_channel == "qq" else feishu_status,
                "switch_key": cfg.switch_key,
                "mcp_tools": cfg.mcp_tools,
                "system_auto_control": cfg.system_auto_control,
                "runtime_status": status.current_status if status else "idle",
                "runtime_tool": status.current_mcp_tool if status else "",
                "latest_mcp_tool": live_tool or (status.current_mcp_tool if status else ""),
                "active_run_status": str(active_run.status or "") if active_run else "",
                "active_run_phase": str(run_live.get("phase") or "idle") if active_run else "idle",
                "active_run_session_id": active_run_session_id,
                "user_chat_active": user_chat_active,
                "recent_user_chat_active": recent_user_chat_active,
                "recent_user_chat_at": recent_user_chat.created_at if recent_user_chat else None,
                "current_task_title": current_task_title,
                "current_task_status": current_task_status,
                "task_current": current_task,
                "task_current_or_recent": current_or_recent_task,
                "task_recent_completed": latest_completed_task,
                "task_scheduled_tasks": scheduled_tasks,
                "latest_thinking": live_reasoning or live_text or persisted_task_thinking,
            }
        )
    return cards


def _ensure_card_configs_enabled(session: Session, user_id: int, cfgs: List[AssistantAIConfig]) -> bool:
    """Repair old stopped configs so dashboards do not expose a dead state."""
    changed = False
    for cfg in cfgs:
        if cfg.enabled:
            continue
        cfg.enabled = True
        cfg.updated_at = time.time()
        session.add(cfg)
        status = session.exec(
            select(AIRuntimeStatus).where(
                AIRuntimeStatus.user_id == user_id,
                AIRuntimeStatus.ai_config_id == cfg.id,
                AIRuntimeStatus.ai_kind == "assistant",
            )
        ).first()
        if not status:
            status = AIRuntimeStatus(user_id=user_id, ai_config_id=cfg.id, ai_kind="assistant")
        status.running = True
        status.updated_at = time.time()
        session.add(status)
        changed = True
    if changed:
        session.commit()
    return changed

@router.post("/sessions")
def create_session(
    body: ChatSessionCreate,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    new_id = f"session_{int(time.time() * 1000)}"
    row = ChatSession(
        user_id=user.id,
        ai_config_id=body.ai_config_id,
        ai_kind=body.ai_kind,
        session_id=new_id,
        session_name=body.session_name,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row
