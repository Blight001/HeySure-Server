"""Chat runtime helpers: resolve the effective AI runtime/config for a request,
load task payloads/jobs by session, manage per-run status and stop flags, and
compute session token totals."""

IS_ROUTER_ENTRY = False

import json
import time
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session, select

from api.database import engine
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


def _digital_society_roster_text(session: Session, user_id: int, self_ai_config_id: int) -> str:
    """组装数字社会成员名单（ID / 名字 / 角色），注入系统 prompt。

    message.send+to+ai 需要对方的 ai_config_id，但普通成员没有任何工具
    可以查询同伴的 ID（admin.manage 门槛是图书馆绑定），
    导致 AI 之间无法互相通信。名单只从 DB 读取，保证 gateway 预览与
    ai-runtime 两进程组装结果一致。
    """
    from mcp_runtime.mcp.permissions import ROLE_LABELS_ZH, config_role_tier

    rows = session.exec(
        select(AssistantAIConfig).where(
            AssistantAIConfig.user_id == user_id,
            AssistantAIConfig.ai_role.in_(["digital_member", "assistant_admin"]),
        ).order_by(AssistantAIConfig.id.asc())
    ).all()
    lines = []
    self_name = ""
    for cfg in rows:
        if str(cfg.lifecycle_status or "") == "dead":
            continue
        cfg_id = int(cfg.id or 0)
        if cfg_id == int(self_ai_config_id):
            self_name = str(cfg.name or "").strip()
            continue
        tier = config_role_tier(cfg)
        label = ROLE_LABELS_ZH.get(tier, tier)
        if bool(getattr(cfg, "is_librarian", False)):
            label += "，图书管理员"
        lines.append(f"- ID {cfg_id}：{cfg.name}（{label}）")
    if not lines:
        return ""
    header = f"你的 ai_config_id 是 {self_ai_config_id}"
    if self_name:
        header += f"（{self_name}）"
    header += (
        "。数字社会中的其他成员如下；用 message.send+to+ai 与他们沟通时，"
        "to_ai_config_id 填对方的 ID（也可用 to_ai_name 填对方名字）："
    )
    return header + "\n" + "\n".join(lines[:100])


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
# 依赖该段 + mcp.describe+tool（tool / tools / query）。
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
        effective_tool_allowlist.add("message.send+to+ai")
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
                    effective_tool_allowlist.add("message.send+to+ai")
            try:
                effective_tool_allowlist |= toolbox_tools_for_config(ai_config_id, uid)
            except Exception:
                pass
            # Even under task override, ensure core system built-ins are directly available
            # (knowledge.search, knowledge.manage, todo.manage, workspace.*, etc. must not be
            # stripped for pre-plan / task flows). Library governance tools are force-included
            # here and the binding filter below will drop unbound ones. This fixes calls to
            # "图书馆 MCP" (e.g. knowledge.manage) being rejected during task execution.
            try:
                from mcp_runtime.mcp import registry as _mcp_registry
                _server_direct = {
                    str(t.get("name") or "").strip()
                    for t in _mcp_registry.list_tools()
                    if t.get("name")
                }
                effective_tool_allowlist |= _server_direct
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

    # System built-in MCPs (from MCP registry) are allowed for direct AI calls.
    # They are NOT gated behind toolbox binding/selection like device (endpoint) MCPs.
    # This fixes "Tool not allowed for this task" for knowledge.* / todo.manage / workspace.* etc.
    # Library governance tools (LIBRARY_BOUND_TOOLS) are included here; the subsequent
    # _filter_tools_for_current_bindings drops them only if the AI is not bound to library.
    # This makes 图书馆 MCP usable in task mode / task runtime when bound.
    try:
        from mcp_runtime.mcp import registry as _mcp_registry
        _server_direct = {
            str(t.get("name") or "").strip()
            for t in _mcp_registry.list_tools()
            if t.get("name")
        }
        effective_tool_allowlist |= _server_direct
    except Exception:
        pass

    # Apply current binding state (library / toolbox) so unbound governance tools
    # do not appear in the visible MCP catalog sent to the model.
    # Note: LIBRARY_BOUND_TOOLS are now force-included by the server_direct adds above
    # (for both normal and task-override flows) so that binding to 图书馆 makes
    # knowledge.manage etc. available even if not explicitly in cfg.mcp_tools or task override.
    # The filter removes them only when not bound.
    effective_tool_allowlist = _filter_tools_for_current_bindings(
        effective_tool_allowlist, uid, ai_config_id
    )
    # 最后再彻底清理一次老名字，防止任何路径残留
    effective_tool_allowlist = fully_clean_tool_names(effective_tool_allowlist)

    if merged_system_prompt:
        system_prompt = merged_system_prompt
    # 数字社会成员名单：让每个 AI 知道同伴的 ai_config_id / 名字，否则
    # message.send+to+ai 无从填 to_ai_config_id（成员查询工具是辅助管理员门槛）。
    if ai_config_id is not None:
        try:
            roster_text = _digital_society_roster_text(session, uid, int(ai_config_id))
        except Exception:
            roster_text = ""
        system_prompt = _strip_prompt_section(system_prompt, "数字社会成员名单")
        if roster_text:
            system_prompt = _append_prompt_section(system_prompt, "数字社会成员名单", roster_text)
    if is_task_runtime:
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

    # 当前工作模式：不改写系统提示（切换模式只在工具结果里返回模式说明），但**决定工具门禁**。
    # 初始对话时，推理会把当前模式的说明作为一条上下文消息注入（等效初始 mode.use 结果），
    # 保证模型从第一轮就看到模式 prompt。默认「初始对话模式」视为「不在工作房间」：
    # 只保留系统自带的基础对话工具（切换模式 / 工具自省 / 收发消息），收走全部设备 / 工作 MCP；
    # 切到 task / learning 等工作模式，系统才把设备 MCP 交回。DB 为准（gateway 预览与 ai-runtime 两进程一致）。
    if ai_config_id is not None:
        try:
            from api.services.mcp.agent_mode_store import (
                is_chat_only_mode,
                mode_allows_device_mcp,
                CHAT_MODE_TOOL_WHITELIST,
            )

            if is_chat_only_mode(uid, ai_config_id):
                keep = set(CHAT_MODE_TOOL_WHITELIST) | set(MCP_INTROSPECTION_TOOLS)
                effective_tool_allowlist = {
                    tool for tool in effective_tool_allowlist if tool in keep
                }
            elif not mode_allows_device_mcp(uid, ai_config_id):
                # 模式类型不允许设备端 MCP：收走桌面 / 浏览器 / 安卓端执行器工具，
                # 服务端工作工具（含真正的 workshop endpoint 工具如 evolution.*）保持不变。
                # 图书馆治理工具 (knowledge.manage 等) 由绑定 + filter 控制，不受此影响。
                from connector_runtime.dispatch.desktop_device_tools import (
                    is_endpoint_agent_tool,
                    is_workshop_tool,
                )

                effective_tool_allowlist = {
                    tool for tool in effective_tool_allowlist
                    if not (is_endpoint_agent_tool(tool) and not is_workshop_tool(tool))
                }
        except Exception:
            pass
    # 剥离历史可能残留的 [当前工作模式] 段（旧设计曾尝试 section 注入，现已统一走工具结果/上下文消息）。
    system_prompt = _strip_prompt_section(system_prompt, "当前工作模式")

    # 这里保留剥离逻辑，让历史注入过目录的存量 prompt / 人格文本就地自愈。
    system_prompt = _strip_prompt_section(system_prompt, "动态 MCP 说明")
    system_prompt = _strip_prompt_section(system_prompt, "可用MCP工具")
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

def _renew_loop_scheduled_job(
    session: Session,
    job: Optional[AITaskJob],
    now: float,
) -> Optional[AITaskJob]:
    """循环任务一轮完成后原地续期同一个 job（回到 queued 等待下一轮）。

    循环未结束时返回续期后的 job；非循环任务或循环已结束（轮数跑满/超
    截止时间）返回 None，由调用方按普通完成流程置 completed。

    下一轮触发时刻由 task_schedule.build_next_loop_schedule 按循环方式
    （interval / daily / weekly）统一计算。同一个 job 贯穿所有轮次，
    轮次之间保持 queued，可正常编辑/暂停/停止，不会变成已完成任务。
    """
    if not job:
        return None
    if str(job.trigger_type or "").strip().lower() != "schedule":
        return None
    try:
        payload = json.loads(job.task_payload) if job.task_payload else {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    from api.services.tasks.task_schedule import build_next_loop_schedule

    next_schedule = build_next_loop_schedule(payload.get("schedule"), now)
    if next_schedule is None:
        return None
    payload["schedule"] = next_schedule
    job.task_payload = json.dumps(payload, ensure_ascii=False)
    job.status = "queued"
    job.finished_at = None
    job.started_at = None
    job.last_supervised_at = None
    job.supervision_count = 0
    # 完成回执的幂等位随轮次重置，下一轮结束时才能再次通知。
    # notify_task_completion 在独立 Session 里已把该字段写成本轮时间，
    # 而当前 Session 的快照仍是 None，直接赋 None 不会被视为变更，
    # 必须强制标脏才能真正写回 NULL。
    job.completion_notified_at = None
    flag_modified(job, "completion_notified_at")
    job.updated_at = now
    session.add(job)
    return job

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
