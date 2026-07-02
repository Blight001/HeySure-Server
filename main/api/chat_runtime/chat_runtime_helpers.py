"""Chat runtime helpers: resolve the effective AI runtime/config for a request,
load task payloads/jobs by session, manage per-run status and stop flags, and
compute session token totals."""

IS_ROUTER_ENTRY = False

import json
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from api.database import engine
from mcp_runtime.mcp import get_project_root
from api.models import AITaskJob, AssistantAIConfig, ChatMessage, ChatRun, User
from api.common.value_utils import safe_json_obj
from api.services.model_presets import resolve_model_preset
from api.services.tasks.task_system import with_workspace_read_by_name_compat
from .run_state import _RUN_LIVE_STATE, _RUN_STATE_LOCK
from .chat_prompt_utils import (
    _append_prompt_section,
    _clear_run_live_text,
    _emit_run_done,
    _filter_tools_for_current_bindings,
    _strip_prompt_section,
    _strip_runtime_injected_sections,
    _strip_task_runtime_sections,
)


def _resolve_ai_runtime(session: Session, user: User, ai_kind: str, ai_config_id: Optional[int]):
    # KnowledgeBase 文件为真相源：建目录 + 首次把现有内容导出成文件（幂等）。
    # 运行时直接读文件（见下方 effective_* 调用），不再回写数据库。
    from api.services.knowledge import kb_store

    kb_store.ensure_user_kb(user.id)
    cfg = None
    if ai_kind in ("assistant", "core"):
        if ai_config_id is None:
            cfg = session.exec(
                select(AssistantAIConfig).where(
                    AssistantAIConfig.user_id == user.id,
                )
            ).first()
        else:
            cfg = session.exec(
                select(AssistantAIConfig).where(
                    AssistantAIConfig.id == ai_config_id,
                    AssistantAIConfig.user_id == user.id,
                )
            ).first()
        if not cfg:
            raise HTTPException(status_code=400, detail="No available assistant AI config")
        api_key, base_url, model = resolve_model_preset(user, cfg)
        # 方案 A：人格 Prompt 直接读 KnowledgeBase/personas/*.md（文件缺失回退 DB）。
        system_prompt = _strip_runtime_injected_sections(kb_store.effective_ai_prompt(user.id, cfg))
        # Show the effective runtime workspace (absolute path), not only raw config text like ".".
        system_prompt = _append_prompt_section(system_prompt, "AI 工作目录", get_project_root(user.id, cfg.id))
        if cfg.database_uri:
            system_prompt = _append_prompt_section(system_prompt, "AI 数据库连接", cfg.database_uri)
    else:
        api_key, base_url, model = resolve_model_preset(user, None)
        system_prompt = _strip_runtime_injected_sections(
            kb_store.effective_system_value(user.id, "admin_prompt")
        )
    if not api_key:
        raise HTTPException(status_code=400, detail="Admin API key not configured")
    if not base_url:
        raise HTTPException(status_code=400, detail="Base URL not configured")
    if not model:
        raise HTTPException(status_code=400, detail="Model not configured")
    if cfg and not cfg.mcp_enabled:
        system_prompt = _append_prompt_section(
            system_prompt,
            "MCP状态",
            "当前 AI 的 MCP 功能未启用。不要调用 MCP 工具；如果任务必须使用 MCP，请说明需要先在该 AI 配置中开启 MCP。",
        )
    return cfg, api_key, base_url, model, system_prompt

# Web 前端勾选工坊/工具组后，会把该组 MCP 工具目录以这个段标题追加进当轮用户
# 消息（model_content），随消息动态携带。系统提示不再注入 [动态 MCP 说明]
# 目录（见 build_runtime_system_prompt_and_tools 内的说明）；模型侧的工具发现
# 依赖该段 + mcp.describe_tool（tool / tools / query）。
CLIENT_MCP_CATALOG_MARKER = "[本轮可用 MCP 工具]"

def build_runtime_system_prompt_and_tools(
    session: Session,
    user: User,
    *,
    ai_kind: str = "assistant",
    ai_config_id: Optional[int] = None,
    session_id: Optional[str] = None,
    merged_system_prompt: Optional[str] = None,
    cfg: Optional[Any] = None,
    base_system_prompt: Optional[str] = None,
    task_payload: Optional[dict] = None,
) -> tuple[str, set]:
    """Single source of truth for the runtime system prompt **and** the effective
    MCP tool allow-list.

    Both the inference worker (``ai_runtime``) and the live ``/system-prompt-preview``
    endpoint (``gateway``) call this, so the prompt shown to the user is assembled by
    the exact same logic the model receives — same MCP discovery hint, same task
    sections. This prevents the two paths from drifting. Note: the full MCP tool
    catalog ([动态 MCP 说明]) is no longer injected into the system prompt; the web
    client attaches the checked tool groups' catalog to the current user message
    instead (see ``CLIENT_MCP_CATALOG_MARKER``).

    ``cfg`` / ``base_system_prompt`` / ``task_payload`` may be passed in by a caller
    that already resolved them (the inference loop) to avoid recomputation; when
    omitted they are resolved here (the preview endpoint).

    Returns ``(system_prompt, effective_tool_allowlist)``.

    ──────────────────────────────────────────────────────────────────────────
    INVARIANT — keep the preview honest (前置 prompt == 真实喂给 AI 的 prompt):
    This function runs in TWO different processes:
      • gateway      → builds the live /system-prompt-preview ("前置 prompt")
      • ai-runtime   → builds the prompt the model actually receives
    Therefore EVERY input that shapes the prompt or the tool allow-list MUST be
    PROCESS-INDEPENDENT — resolve it from the database (DB presence snapshot,
    config rows, bindings), NEVER from in-memory per-process state.

    In particular, DO NOT feed prompt content from the in-memory ``agents``
    socket registry (``get_connected_*_agent`` / ``_iter_agents_for_config`` in
    connector_runtime.dispatch.desktop_device_tools). That registry only exists
    in the process that owns the agent sockets (gateway), so ai-runtime would
    silently drop whatever you add and the preview would lie. Use the DB-backed
    helpers instead (``endpoint_tools_for_config`` / ``endpoint_bridge_tools_for_config``
    / ``toolbox_tools_for_config`` — all read ``api.devices.presence``).

    When you add a NEW prompt section or a NEW tool source below, add it HERE so
    both processes assemble it identically, and verify its data source is the DB.
    ──────────────────────────────────────────────────────────────────────────
    """
    from connector_runtime.dispatch.desktop_device_tools import (
        endpoint_bridge_tools_for_config,
        endpoint_tools_for_config,
        strip_endpoint_tool_config_names,
        toolbox_tools_for_config,
    )
    from connector_runtime.bots import iter_bots as _iter_bots
    from connector_runtime.bots.base import channel_for_session_id as _channel_for_session_id
    from api.services.tasks.task_system import TASK_RUNTIME_REQUIRED_TOOLS, TASK_PLAN_FLOW_PROMPT
    from api.services.mcp.mcp_tool_aliases import fully_clean_tool_names
    from mcp_runtime.mcp.core import MCP_INTROSPECTION_TOOLS
    from api.services.knowledge import kb_store

    uid = user.id
    if cfg is None or base_system_prompt is None:
        cfg, _, _, _, base_system_prompt = _resolve_ai_runtime(session, user, ai_kind, ai_config_id)
    system_prompt = base_system_prompt
    sid = str(session_id or "").strip()
    if task_payload is None:
        task_payload = _load_task_payload_by_session(session, uid, ai_config_id, sid) if sid else {}
    is_task_runtime = bool(task_payload) or sid.startswith("session_task_")

    effective_tool_allowlist = _parse_allowed_tools(cfg.mcp_tools if cfg else None)
    effective_tool_allowlist.update(MCP_INTROSPECTION_TOOLS)
    effective_tool_allowlist.update(endpoint_bridge_tools_for_config(ai_config_id, uid))
    # Endpoint (desktop / browser) tools are governed by the per-(AI, agent-type)
    # permission scope, not cfg.mcp_tools.
    effective_tool_allowlist.update(endpoint_tools_for_config(ai_config_id, uid))
    if ai_config_id is not None:
        # System-injected AI-to-AI messages must remain answerable even when a
        # task or config narrows the general MCP tool allowlist.
        effective_tool_allowlist.add("message.send_to_ai")
    try:
        effective_tool_allowlist |= toolbox_tools_for_config(ai_config_id, uid)
    except Exception:
        pass

    # Per-bot tool requirements (e.g. Feishu adds context-trim) live on the adapter.
    _session_channel = _channel_for_session_id(sid, _iter_bots())
    if _session_channel:
        _bot = next((b for b in _iter_bots() if b.channel == _session_channel), None)
        if _bot is not None:
            effective_tool_allowlist.update(_bot.extra_required_mcp_tools())

    if task_payload:
        override_tools = task_payload.get("override_mcp_tools")
        if isinstance(override_tools, dict) and bool(override_tools.get("enabled")):
            tools = override_tools.get("tools")
            if isinstance(tools, list):
                effective_tool_allowlist = {
                    str(tool).strip() for tool in tools if isinstance(tool, str) and str(tool).strip()
                }
                effective_tool_allowlist = fully_clean_tool_names(effective_tool_allowlist)
                effective_tool_allowlist = strip_endpoint_tool_config_names(
                    with_workspace_read_by_name_compat(effective_tool_allowlist)
                )
                effective_tool_allowlist.update(endpoint_bridge_tools_for_config(ai_config_id, uid))
                effective_tool_allowlist.update(endpoint_tools_for_config(ai_config_id, uid))
                if ai_config_id is not None:
                    effective_tool_allowlist.add("message.send_to_ai")
            try:
                effective_tool_allowlist |= toolbox_tools_for_config(ai_config_id, uid)
            except Exception:
                pass

    # Task runtime must always allow task system tools.
    if is_task_runtime:
        effective_tool_allowlist.update(TASK_RUNTIME_REQUIRED_TOOLS)
    # Dynamic MCP discovery must remain available even when task runtime narrows
    # the operational tool allowlist.
    effective_tool_allowlist.update(MCP_INTROSPECTION_TOOLS)

    # Server toolbox MCP tools come from the toolbox DeviceMcpScope.
    try:
        effective_tool_allowlist |= toolbox_tools_for_config(ai_config_id, uid)
    except Exception:
        pass

    # Apply current binding state (library / toolbox) so unbound governance tools
    # do not appear in the visible MCP catalog sent to the model.
    effective_tool_allowlist = _filter_tools_for_current_bindings(
        effective_tool_allowlist, uid, ai_config_id
    )
    # 最后再彻底清理一次老名字，防止任何路径残留
    effective_tool_allowlist = fully_clean_tool_names(effective_tool_allowlist)

    if merged_system_prompt:
        system_prompt = merged_system_prompt
    if is_task_runtime:
        # Keep only one effective workspace section in task runtime prompt.
        system_prompt = _append_prompt_section(
            _strip_prompt_section(system_prompt, "AI 工作目录"),
            "AI 工作目录",
            get_project_root(uid, ai_config_id),
        )
        # Remove legacy task-runtime prompt sections; task constraints are enforced server-side.
        system_prompt = _strip_task_runtime_sections(system_prompt)
        # Steer the planned task flow: plan -> phased execution -> summarized end.
        task_plan_flow_text = kb_store.effective_system_value(
            uid, "task_plan_flow_prompt", TASK_PLAN_FLOW_PROMPT
        ).strip() or TASK_PLAN_FLOW_PROMPT
        system_prompt = _append_prompt_section(
            _strip_prompt_section(system_prompt, "任务规划流程"),
            "任务规划流程",
            task_plan_flow_text,
        )

    # [动态 MCP 说明] 目录已从系统提示中卸载：工具目录改为由 Web 前端在勾选
    # 工坊/工具组后随当轮用户消息携带（段标题 CLIENT_MCP_CATALOG_MARKER），
    # 未携带时模型通过 mcp.describe_tool（tool / tools / query）按需发现工具。
    # 这里保留剥离逻辑，让历史注入过目录的存量 prompt / 人格文本就地自愈。
    system_prompt = _strip_prompt_section(system_prompt, "动态 MCP 说明")
    system_prompt = _strip_prompt_section(system_prompt, "可用MCP工具")

    if bool(effective_tool_allowlist) and (cfg is None or getattr(cfg, "mcp_enabled", False)):
        system_prompt = _append_prompt_section(
            _strip_prompt_section(system_prompt, "MCP 工具发现"),
            "MCP 工具发现",
            "工具目录不再内置于系统提示。当轮用户消息若附带 [本轮可用 MCP 工具] 段，"
            "优先从该目录定位工具；否则用 mcp.describe_tool 发现工具"
            "（tool 单个 / tools 批量 / query 关键词搜索），取到参数 schema 后即可直接调用。",
        )
    return system_prompt, effective_tool_allowlist


def build_effective_system_prompt(
    session: Session,
    user: User,
    *,
    ai_kind: str = "assistant",
    ai_config_id: Optional[int] = None,
    session_id: Optional[str] = None,
    merged_system_prompt: Optional[str] = None,
) -> str:
    """Build the same runtime system prompt the inference loop injects before a turn.

    Thin wrapper over :func:`build_runtime_system_prompt_and_tools` (single source
    of truth) that returns only the prompt text.
    """
    prompt, _tools = build_runtime_system_prompt_and_tools(
        session,
        user,
        ai_kind=ai_kind,
        ai_config_id=ai_config_id,
        session_id=session_id,
        merged_system_prompt=merged_system_prompt,
    )
    return prompt

def _parse_allowed_tools(raw: Optional[str]) -> set[str]:
    from connector_runtime.dispatch.desktop_device_tools import strip_endpoint_tool_config_names
    from api.services.mcp.mcp_tool_aliases import fully_clean_tool_names

    try:
        parsed = json.loads(raw or "[]")
        if not isinstance(parsed, list):
            return set()
        raw_tools = {str(item).strip() for item in parsed if isinstance(item, str) and str(item).strip()}
        # 彻底清理：归一旧名 + 强制剔除任何残留的老名字，确保 prompt 里干净
        raw_tools = fully_clean_tool_names(raw_tools)
        return strip_endpoint_tool_config_names(with_workspace_read_by_name_compat(raw_tools))
    except Exception:
        return set()

def _load_task_payload_by_session(
    session: Session,
    user_id: int,
    ai_config_id: Optional[int],
    session_id: str,
) -> Dict[str, Any]:
    if ai_config_id is None:
        return {}
    row = session.exec(
        select(AITaskJob).where(
            AITaskJob.user_id == user_id,
            AITaskJob.ai_config_id == ai_config_id,
            AITaskJob.session_id == session_id,
        ).order_by(AITaskJob.updated_at.desc())
    ).first()
    if not row:
        return {}
    return safe_json_obj(row.task_payload)

def _load_task_job_by_session(
    session: Session,
    user_id: int,
    ai_config_id: Optional[int],
    session_id: str,
) -> Optional[AITaskJob]:
    if ai_config_id is None:
        return None
    return session.exec(
        select(AITaskJob).where(
            AITaskJob.user_id == user_id,
            AITaskJob.ai_config_id == ai_config_id,
            AITaskJob.session_id == session_id,
        ).order_by(AITaskJob.updated_at.desc())
    ).first()

def _is_task_finished_status(status: str) -> bool:
    return str(status or "").strip() in {"completed", "cancelled", "stopped", "error"}

def _create_loop_scheduled_job(
    session: Session,
    source_job: Optional[AITaskJob],
    now: float,
) -> Optional[AITaskJob]:
    """循环任务完成后创建下一轮实例；循环已结束（轮数跑满/超截止时间）返回 None。

    下一轮触发时刻由 task_schedule.build_next_loop_schedule 按循环方式
    （interval / daily / weekly）统一计算。
    """
    if not source_job:
        return None
    if str(source_job.trigger_type or "").strip().lower() != "schedule":
        return None
    try:
        payload = json.loads(source_job.task_payload) if source_job.task_payload else {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    from api.services.tasks.task_schedule import build_next_loop_schedule

    next_schedule = build_next_loop_schedule(payload.get("schedule"), now)
    if next_schedule is None:
        return None
    payload["schedule"] = next_schedule
    next_job = AITaskJob(
        job_id=f"job_{uuid.uuid4().hex[:12]}",
        user_id=source_job.user_id,
        ai_config_id=source_job.ai_config_id,
        created_by_ai_config_id=source_job.created_by_ai_config_id,
        created_by_session_id=source_job.created_by_session_id,
        ai_kind=source_job.ai_kind or "core",
        template_id=source_job.template_id,
        title=source_job.title,
        instruction=source_job.instruction,
        task_payload=json.dumps(payload, ensure_ascii=False),
        priority=max(1, min(10, int(source_job.priority or 5))),
        status="queued",
        trigger_type="schedule",
    )
    session.add(next_job)
    return next_job

def _run_set_status(run_id: str, status: str, error_message: Optional[str] = None, finished: bool = False):
    with Session(engine) as bg:
        row = bg.exec(select(ChatRun).where(ChatRun.run_id == run_id)).first()
        if not row:
            return
        if row.stop_requested and status != "stopped":
            status = "stopped"
            error_message = row.error_message or error_message
            finished = True
        row.status = status
        row.error_message = error_message
        row.updated_at = time.time()
        if row.started_at is None and status == "running":
            row.started_at = time.time()
        if finished:
            row.finished_at = time.time()
        bg.add(row)
        bg.commit()
        # Snapshot the fields the terminal event needs while the row is loaded;
        # the Session closes when the with-block exits.
        done_payload = {
            "run_id": run_id,
            "user_id": row.user_id,
            "status": row.status,
            "error_message": row.error_message,
            "session_id": row.session_id,
            "ai_config_id": row.ai_config_id,
            "ai_kind": row.ai_kind,
        } if finished else None
    if finished:
        _clear_run_live_text(run_id)
        if done_payload:
            _emit_run_done(**done_payload)

def _run_should_stop(run_id: str) -> bool:
    with Session(engine) as bg:
        row = bg.exec(select(ChatRun).where(ChatRun.run_id == run_id)).first()
        return bool(row and row.stop_requested)

def _session_total_tokens(
    session: Session,
    user_id: int,
    ai_kind: str,
    session_id: str,
    ai_config_id: Optional[int],
) -> int:
    stmt = select(ChatMessage).where(
        ChatMessage.user_id == user_id,
        ChatMessage.ai_kind == ai_kind,
        ChatMessage.session_id == session_id,
    )
    if ai_config_id is not None:
        stmt = stmt.where(ChatMessage.ai_config_id == ai_config_id)
    rows = session.exec(stmt).all()
    persisted_total = int(sum(int(r.total_tokens or 0) for r in rows))

    active_runs = session.exec(
        select(ChatRun).where(
            ChatRun.user_id == user_id,
            ChatRun.ai_kind == ai_kind,
            ChatRun.session_id == session_id,
            ChatRun.status.in_(["queued", "running"]),
        )
    ).all()
    pending_total = 0
    with _RUN_STATE_LOCK:
        for run in active_runs:
            if ai_config_id is not None and run.ai_config_id != ai_config_id:
                continue
            pending_total += int((_RUN_LIVE_STATE.get(run.run_id) or {}).get("pending_total_tokens") or 0)
    return int(persisted_total + pending_total)

def _live_pending_tokens_for(
    session: Session,
    *,
    user_id: int,
    ai_kind: str,
    ai_config_id: Optional[int] = None,
    session_id: Optional[str] = None,
) -> Dict[str, int]:
    stmt = select(ChatRun).where(
        ChatRun.user_id == user_id,
        ChatRun.ai_kind == ai_kind,
        ChatRun.status.in_(["queued", "running"]),
    )
    if ai_config_id is not None:
        stmt = stmt.where(ChatRun.ai_config_id == ai_config_id)
    if session_id is not None:
        stmt = stmt.where(ChatRun.session_id == session_id)
    runs = session.exec(stmt).all()
    prompt = 0
    completion = 0
    total = 0
    with _RUN_STATE_LOCK:
        for run in runs:
            live = _RUN_LIVE_STATE.get(run.run_id) or {}
            prompt += int(live.get("pending_prompt_tokens") or 0)
            completion += int(live.get("pending_completion_tokens") or 0)
            total += int(live.get("pending_total_tokens") or 0)
    return {
        "prompt_tokens": int(prompt),
        "completion_tokens": int(completion),
        "total_tokens": int(total),
    }
