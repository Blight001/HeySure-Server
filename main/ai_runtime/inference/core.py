IS_ROUTER_ENTRY = False

import asyncio
import json
import logging
import time


logger = logging.getLogger(__name__)
from typing import Any, Dict, List, Optional

import requests
from sqlmodel import Session, select

from api.database import engine
from api.runtime import http_client, run_context
ai_http_post = http_client.ai_http_post
from mcp_runtime.mcp import get_project_root
from mcp_runtime.mcp.core import MCP_INTROSPECTION_TOOLS
from api.models import AITaskJob, ChatMessage, ChatMessageCreate, User
from api.services.chat import chat_inject, mcp_session_context
from api.services.tasks import task_plan as plan_service
from ai_runtime.inference import ai_message_service
from ai_runtime.inference import compression_flow
from ai_runtime.inference import phase_context
from ai_runtime.inference import plan_transitions
from ai_runtime.inference import step_preparation
from ai_runtime.inference import tool_media
from ai_runtime.inference import tool_metadata
from ai_runtime.inference import tool_persistence
from ai_runtime.inference import tool_rejections
from ai_runtime.inference.debug_support import (
    ai_color as _ai_color,
    ai_debug_enabled as _ai_debug_enabled,
    ai_debug_log as _ai_debug_log,
    ai_debug_stage as _ai_debug_stage,
    ai_short as _ai_short,
    ai_short_base_url as _ai_short_base_url,
    ai_short_run_id as _ai_short_run_id,
    heysure_provider_session_id as _heysure_provider_session_id,
)
from ai_runtime.inference.conversation_history import build_conversation_history
from ai_runtime.inference.policies import (
    can_start_inference_step as _can_start_inference_step,
    coerce_max_steps as _coerce_max_steps,
    has_active_todo_plan as _has_active_todo_plan,
)
from ai_runtime.inference.plan_flow import (
    PlanFinalizeContext,
    append_plan_directive,
    finalize_plan,
    send_task_completion_notification as _notify_task_completion,
)
from ai_runtime.inference.tool_resolution import (
    ToolResponseContext,
    TurnCallAction,
    append_joined_tool_response as _append_joined_tool_response,
    append_ordinary_tool_response as _append_ordinary_tool_response,
    append_pending_call_responses as _answer_pending_calls,
    flush_screenshot_messages as _flush_screenshot_messages,
    missing_required_mcp_args as _missing_required_mcp_args,
    resolve_mcp_tool_name as _resolve_mcp_tool_name,
    split_concatenated_native_tool_name as _split_concatenated_native_tool_name,
    to_native_tool_name as _to_native_tool_name,
)
from ai_runtime.inference.run_request import (
    WorkerRequest,
    resolve_session_preset_entry,
    start_worker_run,
)
from ai_runtime.inference.tool_execution import (
    JoinedToolRequest,
    execute_tool_call as _execute_tool_call,
)
get_run_session_context = run_context.get_run_session_context
set_run_session_context = run_context.set_run_session_context
from connector_runtime.dispatch.desktop_device_tools import (
    endpoint_bridge_tools_for_config,
    endpoint_tools_for_config,
)
from api.services.tasks.task_system import (
    TASK_PLAN_FLOW_PROMPT,
    TASK_RUNTIME_REQUIRED_TOOLS,
    normalize_system_auto_control,
    with_workspace_read_by_name_compat,
)
from api.chat_runtime.chat_prompt_utils import (
    _append_mcp_state_to_tags,
    _append_prompt_section,
    _build_mcp_stream_warning,
    _extract_mcp_error,
    _filter_tools_for_current_bindings,
    _safe_json,
    _set_run_live_meta,
    _set_run_live_phase,
    _set_run_live_text,
    _set_run_live_usage,
    _strip_prompt_section,
    _strip_task_runtime_sections,
)
from api.services.chat.chat_persistence import _save_message
from api.chat_runtime.chat_stream import _detect_provider, stream_turn_anthropic, stream_turn_openai_compat
from api.chat_runtime.chat_runtime_helpers import (
    _is_task_finished_status,
    _load_task_job_by_session,
    _load_task_payload_by_session,
    _renew_loop_scheduled_job,
    _parse_allowed_tools,
    _resolve_ai_runtime,
    _run_set_status,
    _run_should_stop,
    build_runtime_system_prompt_and_tools,
)

from api.core.config import DEFAULT_CHAT_MAX_STEPS
from api.core.settings import settings


def _format_upstream_error(response: requests.Response, max_body_len: int = 4000) -> str:
    status = f"HTTP {response.status_code}"
    reason = str(response.reason or "").strip()
    if reason:
        status = f"{status} {reason}"

    body = str(response.text or "").strip()
    if body:
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                error = parsed.get("error")
                if isinstance(error, dict):
                    message = str(error.get("message") or "").strip()
                    code = str(error.get("code") or "").strip()
                    error_type = str(error.get("type") or "").strip()
                    parts = [part for part in [message, code, error_type] if part]
                    if parts:
                        body = " | ".join(parts)
                elif isinstance(error, str) and error.strip():
                    body = error.strip()
        except Exception:
            pass
    if len(body) > max_body_len:
        body = f"{body[:max_body_len]}\n...<truncated>"
    return f"Upstream AI request failed: {status} for {response.url}\n{body}".strip()


def _raise_for_upstream_error(response: requests.Response) -> None:
    if response.ok:
        return
    raise RuntimeError(_format_upstream_error(response))


def _record_mcp_call(record: tool_persistence.ToolCallRecord) -> None:
    tool_persistence.record_tool_call(record)


def _duplicate_call_flags(turn_calls: List[Dict[str, Any]]) -> List[bool]:
    """Flag each call that exactly repeats an earlier call in the same turn.

    A duplicate has the same tool name and the same arguments (compared as
    canonical, key-sorted JSON). The first occurrence of a signature is
    ``False``; every later identical one is ``True``.

    Models occasionally emit several identical tool calls in a single turn.
    The worker executes only the first and answers the rest without re-running
    them, so a side-effecting tool (send message, click, submit) does not fire
    twice off one hiccup. Order is preserved so callers can align the flags
    with ``turn_calls`` by index.
    """
    seen: set[str] = set()
    flags: List[bool] = []
    for call in turn_calls:
        sig = (
            f"{call.get('tool') or ''}|"
            f"{json.dumps(call.get('arguments') or {}, ensure_ascii=False, sort_keys=True)}"
        )
        flags.append(sig in seen)
        seen.add(sig)
    return flags


def _append_missing_tool_responses(convo: List[Dict], error_text: str) -> List[str]:
    """Repair OpenAI-style history in-place.

    OpenAI-compatible providers require every assistant message with tool_calls
    to be followed immediately by tool messages for each tool_call_id. Appending
    synthetic tool responses to the end is still invalid if a user/system message
    already sits between the assistant tool_calls and the tool response, so the
    repair inserts missing tool messages at the exact required position.
    """
    repaired_ids: List[str] = []
    idx = 0
    while idx < len(convo):
        item = convo[idx]
        if item.get("role") != "assistant" or not item.get("tool_calls"):
            if item.get("role") == "tool":
                # Orphan tool messages are invalid in OpenAI-compatible payloads.
                # They can appear after an older failed repair appended a tool
                # response behind a user notice. Drop them from the outgoing
                # in-memory request; persisted user/assistant history is untouched.
                convo.pop(idx)
                continue
            idx += 1
            continue

        tool_calls = item.get("tool_calls") or []
        expected_ids = [
            str(call.get("id") or "").strip()
            for call in tool_calls
            if isinstance(call, dict) and str(call.get("id") or "").strip()
        ]
        if not expected_ids:
            idx += 1
            continue

        seen_ids = set()
        insert_at = idx + 1
        while insert_at < len(convo) and convo[insert_at].get("role") == "tool":
            tool_call_id = str(convo[insert_at].get("tool_call_id") or "").strip()
            if tool_call_id in expected_ids and tool_call_id not in seen_ids:
                seen_ids.add(tool_call_id)
                insert_at += 1
                continue
            convo.pop(insert_at)

        missing_ids = [tool_call_id for tool_call_id in expected_ids if tool_call_id not in seen_ids]
        for offset, tool_call_id in enumerate(missing_ids):
            convo.insert(insert_at + offset, {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": _safe_json({
                    "success": False,
                    "error": error_text,
                    "recovered": True,
                }),
            })
        repaired_ids.extend(missing_ids)
        idx = insert_at + len(missing_ids)
    return repaired_ids


def _mcp_tool_device_identity(
    tool: str,
    user_id: int,
    tool_result: Optional[Dict[str, object]],
) -> tuple[str, str]:
    return tool_persistence.tool_device_identity(tool, user_id, tool_result)


def _build_mcp_tool_bubble_content(
    tool: str,
    arguments: dict,
    result_text: str,
    failed: bool = False,
    image_url: str = "",
    *,
    device_id: str = "",
    device_name: str = "",
) -> str:
    return tool_persistence.build_tool_bubble_content(
        tool,
        arguments,
        result_text,
        failed,
        image_url,
        device_id=device_id,
        device_name=device_name,
    )


def _save_mcp_tool_bubble(request: tool_persistence.ToolBubbleRequest) -> None:
    tool_persistence.save_tool_bubble(request)


def _extract_screenshot_bubble_url(content: str) -> str:
    return tool_persistence.extract_screenshot_bubble_url(content)


def _is_image_input_unsupported_error(error_text: str) -> bool:
    return tool_media.is_image_input_unsupported_error(error_text)


def _degrade_image_messages_to_text(convo: List[Dict]) -> int:
    return tool_media.degrade_image_messages_to_text(convo)


def _prune_prior_runtime_screenshot_images(convo: List[Dict]) -> int:
    return tool_media.prune_prior_runtime_screenshot_images(convo)


def _image_input_degraded_feedback(error_text: str, removed_images: int) -> str:
    return tool_media.image_input_degraded_feedback(error_text, removed_images)


def _find_image_payload(value: object, depth: int = 0) -> Dict[str, str]:
    return tool_media.find_image_payload(value, depth)


def _image_path_to_data_url(path: str) -> str:
    return tool_media.image_path_to_data_url(path)


def _omit_image_fields(value: object) -> object:
    return tool_media.omit_image_fields(value)


def _tool_image_message(tool: str, tool_result: Dict[str, object]) -> Optional[Dict[str, object]]:
    return tool_media.tool_image_message(tool, tool_result)


def _browser_screenshot_image_message(tool: str, tool_result: Dict[str, object]) -> Optional[Dict[str, object]]:
    return _tool_image_message(tool, tool_result)


def _screenshot_display_ref(tool: str, tool_result: Dict[str, object]) -> Dict[str, str]:
    return tool_media.screenshot_display_ref(tool, tool_result)


def _find_screenshot_result_payload(value: object, depth: int = 0) -> Dict[str, object]:
    return tool_media.find_screenshot_result_payload(value, depth)


def _screenshot_send_to_user_enabled(tool: str, tool_result: Dict[str, object], args: Optional[dict] = None) -> bool:
    return tool_media.screenshot_send_to_user_enabled(tool, tool_result, args)


def _bot_target_from_route(route: object) -> Dict[str, object]:
    return tool_persistence._bot_target_from_route(route)


def _deliver_screenshot_to_bot(bg: Session, message: ChatMessage, *, tool_result: Dict[str, object]) -> Dict[str, object]:
    return tool_persistence._send_screenshot_to_bot(bg, message, tool_result)


def _model_visible_tool_result(
    tool: str,
    tool_result: Dict[str, object],
    *,
    image_attached: bool = True,
) -> object:
    return tool_media.model_visible_tool_result(
        tool,
        tool_result,
        image_attached=image_attached,
    )


def _run_worker(
    *,
    run_id: str,
    user_id: int,
    ai_config_id: Optional[int],
    ai_kind: str,
    session_id: str,
    session_name: str,
    model_user_content: Optional[str] = None,
    merged_system_prompt: Optional[str] = None,
    max_steps: Optional[int] = None,
    current_user_message_id: Optional[int] = None,
    selected_mcp_tools: Optional[set[str]] = None,
):
    """Public worker entry. Wraps the implementation with heartbeat lifecycle
    so every caller (monolith thread, ai-runtime dispatcher, scheduler) gets
    watchdog protection without needing to spawn its own heartbeat thread.
    """
    import threading as _threading
    from api.runtime import heartbeat as _hb

    _stop_hb = _threading.Event()

    def _tick_loop() -> None:
        while not _stop_hb.is_set():
            try:
                _hb.tick(run_id)
            except Exception:
                pass
            if _stop_hb.wait(_hb.TICK_INTERVAL_SECONDS):
                return

    _hb_thread = _threading.Thread(target=_tick_loop, name=f"hb-{run_id}", daemon=True)
    _hb_thread.start()

    # Mirror the live answer to a QQ streaming message when applicable. The
    # session (if any) owns final delivery for this run, so we finalize it in
    # the finally block regardless of how the run ends.
    try:
        from connector_runtime.bots.qq.stream_sender import maybe_start_qq_stream
        maybe_start_qq_stream(
            run_id=run_id,
            user_id=user_id,
            ai_config_id=ai_config_id,
            ai_kind=ai_kind,
            session_id=session_id,
        )
    except Exception:
        pass

    try:
        _run_worker_impl(WorkerRequest.create(
            run_id=run_id,
            user_id=user_id,
            ai_config_id=ai_config_id,
            ai_kind=ai_kind,
            session_id=session_id,
            session_name=session_name,
            model_user_content=model_user_content,
            merged_system_prompt=merged_system_prompt,
            max_steps=max_steps,
            current_user_message_id=current_user_message_id,
            selected_mcp_tools=selected_mcp_tools,
        ))
    finally:
        try:
            from connector_runtime.bots.qq.stream_sender import finish_qq_stream
            finish_qq_stream(run_id, session_id=session_id)
        except Exception:
            pass
        # Race backstop: a user-inject that landed after the loop's last drain
        # but before this run committed "completed" would otherwise sit forever.
        # resume_orphaned_injects self-guards (no live run + still pending) and
        # spins up a continuation run to answer it.
        try:
            chat_inject.resume_orphaned_injects(
                user_id=user_id,
                ai_config_id=ai_config_id,
                ai_kind=ai_kind,
                session_id=session_id,
                session_name=session_name,
            )
        except Exception:
            logger.exception("resume orphaned user-injects failed")
        _stop_hb.set()
        _hb_thread.join(timeout=1.0)


def _run_worker_impl(request: WorkerRequest):
    if not start_worker_run(request):
        return
    (
        run_id, user_id, ai_config_id, ai_kind, session_id, session_name,
        model_user_content, merged_system_prompt, max_steps,
        current_user_message_id, selected_mcp_tools,
    ) = request.unpack()
    try:
        with Session(engine) as bg:
            user = bg.get(User, user_id)
            if not user:
                raise RuntimeError("User not found")
            max_steps = _coerce_max_steps(
                max_steps,
                _coerce_max_steps(getattr(user, "mcp_max_steps", DEFAULT_CHAT_MAX_STEPS), DEFAULT_CHAT_MAX_STEPS),
            )
            from api.services.knowledge import kb_store

            cfg, api_key, base_url, model, system_prompt = _resolve_ai_runtime(
                bg, user, ai_kind, ai_config_id, session_id
            )
            # 方案 A：系统提示 / 人格自动控制直接读 KnowledgeBase 文件（缺失回退 DB）。
            mcp_warning_template = kb_store.effective_system_value(
                user_id, "mcp_format_error_hint", getattr(user, "mcp_format_error_hint", "")
            ).strip()
            # 「文本含工具类标记但一个调用都没解析出来」的兜底警告每个 run 只允许
            # 一次：真格式错误一次提醒就够，纯讨论工具语法的正文则不该反复触发。
            markup_fallback_available = True
            auto_ctl = normalize_system_auto_control(
                kb_store.effective_auto_control_json(user_id, cfg) if cfg else None
            )
            compression_failed = False
            task_payload = _load_task_payload_by_session(bg, user_id, ai_config_id, session_id)
            task_job = _load_task_job_by_session(bg, user_id, ai_config_id, session_id)
            is_task_runtime = bool(task_payload) or str(session_id or "").startswith("session_task_")

            # Single source of truth shared with the live /system-prompt-preview
            # endpoint: identical MCP catalog, discovery guidance and task sections,
            # so the prompt shown to the user is exactly the prompt the model receives.
            system_prompt, effective_tool_allowlist = build_runtime_system_prompt_and_tools(
                bg,
                user,
                ai_kind=ai_kind,
                ai_config_id=ai_config_id,
                session_id=session_id,
                merged_system_prompt=merged_system_prompt,
                cfg=cfg,
                base_system_prompt=system_prompt,
                task_payload=task_payload,
                selected_mcp_tools=selected_mcp_tools,
            )

            msg_stmt = select(ChatMessage).where(
                ChatMessage.user_id == user_id,
                ChatMessage.session_id == session_id,
                ChatMessage.ai_kind == ai_kind,
            ).order_by(ChatMessage.created_at.asc())
            if ai_config_id is not None:
                msg_stmt = msg_stmt.where(ChatMessage.ai_config_id == ai_config_id)
            history = bg.exec(msg_stmt).all()
            # Compaction is a permanent safety valve now (no per-user toggle): the
            # cap only shortens a historical tool result that exceeds it, so with a
            # generous cap ordinary results replay in full while a giant dump can
            # never blow up the context window.
            mcp_history_result_max_chars = max(
                20,
                min(10000, int(getattr(user, "mcp_history_result_max_chars", 8000) or 8000)),
            )
            convo = build_conversation_history(
                history,
                system_prompt=system_prompt,
                mcp_result_max_chars=mcp_history_result_max_chars,
                model_user_content=model_user_content,
            )

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                # Unknown HTTP headers are ignored by ordinary providers, while
                # HeySure's local CLI gateways use this anonymous value to map
                # full OpenAI requests onto one provider-side conversation.
                "X-HeySure-Session-ID": _heysure_provider_session_id(
                    user_id, ai_config_id, ai_kind, session_id
                ),
            }
            last_rejected_tool_sig = ""
            rejected_repeat = 0
            consecutive_ai_errors = 0
            image_input_disabled = False
            # 跨步「原地重放」检测：连续若干步产出完全相同的工具批次（同名同参数）
            # 说明模型卡住了——同一查询不会给出不同结果，继续执行只是空烧步数。
            last_batch_sig = ""
            consecutive_same_batch = 0

            # Native tool schemas are exposed progressively. Keep the full
            # allowlist as the execution boundary, but initially show only MCP
            # self-inspection tools to the model.
            mcp_active = bool(cfg and cfg.mcp_enabled and effective_tool_allowlist)
            restored_described_tools: set[str] = set()
            if ai_config_id is not None:
                try:
                    cached_versions = mcp_session_context.described_tool_versions(
                        bg,
                        user_id=user_id,
                        ai_config_id=ai_config_id,
                        ai_kind=ai_kind,
                        session_id=session_id,
                    )
                    if cached_versions:
                        from tools.introspection import current_tool_schema_versions

                        current_versions = current_tool_schema_versions(user_id, cached_versions.keys())
                        restored_described_tools = {
                            name for name, version in cached_versions.items()
                            if current_versions.get(name) == version and name in effective_tool_allowlist
                        }
                except Exception:
                    logger.exception("restore described MCP tools failed")
            exposed_tool_allowlist = (
                (set(MCP_INTROSPECTION_TOOLS) | restored_described_tools)
                & set(effective_tool_allowlist)
            )
            if is_task_runtime:
                # Pre-expose the task / planned-flow tools so a task runtime can
                # plan, advance phases and finish without a describe_tool detour.
                exposed_tool_allowlist |= set(TASK_RUNTIME_REQUIRED_TOOLS) & set(effective_tool_allowlist)
            provider = _detect_provider(base_url)
            # Explicit preset capability fields beat base_url sniffing: a local
            # CLI gateway (grok-cli 等) looks like a generic OpenAI endpoint, so
            # the preset is the only place that knows the wire/tool protocol.
            preset_entry = resolve_session_preset_entry(
                bg, user, cfg, session_id, ai_kind
            ) or {}
            preset_provider = str(preset_entry.get("provider") or "auto")
            if preset_provider == "anthropic":
                provider = "anthropic"
            elif preset_provider == "openai":
                provider = "openai_compat"
            tool_protocol = str(preset_entry.get("tool_protocol") or "auto")
            _ai_debug_stage(
                "INIT",
                f"{_ai_short_run_id(run_id)} {provider} {model} tc_proto={tool_protocol} "
                f"host={_ai_short_base_url(base_url)} "
                f"hist={len(history)} tools={len(effective_tool_allowlist)}/{len(exposed_tool_allowlist)} "
                f"mcp={'on' if mcp_active else 'off'}",
                "34",
            )

            # Expose session context to MCP tools (e.g. admin.dispatch_task) so
            # async desktop-agent results can be appended to this session. The
            # worker runs in its own thread, so the contextvar is naturally scoped.
            set_run_session_context({
                "user_id": user_id,
                "ai_config_id": ai_config_id,
                "ai_kind": ai_kind,
                "session_id": session_id,
                "session_name": session_name,
                "model": model,
                "current_user_message_id": current_user_message_id,
            })

            pending_ai_reply_message_id = ""
            # Planned todo flow state. ``plan_state`` is non-None only while an
            # active plan exists for this (ai, session); it drives per-phase
            # context compaction. ``phase_start_convo_index`` marks where the
            # current phase begins in the live convo; ``phase_started_at`` marks
            # the wall-clock boundary used to tag persisted messages on phase
            # completion; ``phase_mcp_statuses`` records each tool's outcome so a
            # finished phase collapses to a compact status line.
            plan_state = None
            phase_start_convo_index = len(convo)
            phase_started_at = time.time()
            phase_mcp_statuses: List[tuple] = []
            # Plan mode is optional for task runtimes. If the AI chooses to
            # create a todo plan, the system drives phase transitions and finish
            # handling from that point onward.
            flow_awaiting_finish = False
            # A directive to inject *after* the current tool result is appended
            # (so a native tool_call keeps its matching tool response adjacent).
            pending_flow_directive = ""
            # Load any active plan for this (ai, session). A plan can exist in a
            # normal conversation too — not only a task runtime — so the lookup
            # is unconditional. Without it, a rebuilt run (fresh worker call or
            # post-compression) loses the plan thread and the model re-plans from
            # phase 1 (which abandons the live plan via todo.manage create).
            if ai_config_id is not None:
                try:
                    plan_state = plan_service.get_active_plan(
                        bg, user_id, int(ai_config_id), session_id
                    )
                    flow_awaiting_finish = plan_service.awaiting_finish(bg, plan_state)
                except Exception:
                    logger.exception("plan state load failed")
                    plan_state = None

            def _inject_flow_directive(target_convo: List[Dict]) -> None:
                append_plan_directive(
                    target_convo,
                    bg,
                    plan_state,
                    awaiting_finish=flow_awaiting_finish,
                )

            def _auto_finalize_plan(final_phase_since_ts: float) -> None:
                nonlocal plan_state, flow_awaiting_finish
                if plan_state is None:
                    return
                finalize_plan(
                    PlanFinalizeContext(
                        session=bg,
                        user_id=user_id,
                        ai_config_id=int(ai_config_id),
                        ai_kind=ai_kind,
                        session_id=session_id,
                        session_name=session_name,
                        model=model,
                        task_job=task_job,
                    ),
                    plan_state,
                    final_phase_since_ts=final_phase_since_ts,
                )
                plan_state = None
                flow_awaiting_finish = False

            # Tell the AI where it stands. With an active plan, re-anchor on the
            # current phase. Without one, only a task runtime is nudged to plan
            # (plan mode stays optional/AI-driven in normal conversations).
            # A resumed run whose plan already closed its last phase is finalized
            # here without forcing any separate finish call.
            _was_awaiting_finish_on_load = flow_awaiting_finish
            if flow_awaiting_finish:
                _auto_finalize_plan(phase_started_at)
            if plan_state is not None:
                _inject_flow_directive(convo)
            elif is_task_runtime and ai_config_id is not None and not _was_awaiting_finish_on_load:
                convo.append({"role": "user", "content": phase_context.render_plan_required_notice()})

            def _flow_allowed_tool(tool_name: str) -> bool:
                """Hard gate: which tools the planned flow permits right now."""
                if plan_state is None:
                    return True
                name = str(tool_name or "")
                if name in MCP_INTROSPECTION_TOOLS:
                    return True
                if flow_awaiting_finish:
                    return name == "todo.manage"
                return True

            def _execute_turn_call(
                call: Dict[str, Any],
                pending: List[Dict[str, Any]],
            ) -> TurnCallAction:
                """Run one tool call from the current turn's batch.

                Returns an explicit ``TurnCallAction`` transition for the batch
                driver. Control-flow tools never rely on free-form strings.

                Control-flow tools (context clear/compress, plan create/edit/
                delete) rebuild or truncate ``convo``, so they end the batch;
                ``pending`` is answered first so no tool_call_id is orphaned.
                """
                nonlocal session_name, last_rejected_tool_sig, rejected_repeat
                nonlocal plan_state, flow_awaiting_finish
                nonlocal phase_start_convo_index, phase_started_at, phase_mcp_statuses

                tool = str(call.get("tool") or "")
                arguments = call.get("arguments") or {}
                call_id = str(call.get("id") or "call_0")

                if _run_should_stop(run_id):
                    _run_set_status(run_id, "stopped", finished=True)
                    return TurnCallAction.STOP_RUN

                rejection_context = tool_rejections.RejectionContext(
                    session=bg, conversation=convo, pending=pending,
                    saved_message=saved, user_id=user_id,
                    ai_config_id=ai_config_id, ai_kind=ai_kind,
                    session_id=session_id, session_name=session_name,
                    model=model, run_id=run_id, native_tool_calls=_has_native_tc,
                    set_live_phase=lambda phase: _set_run_live_phase(run_id, phase),
                    set_run_error=lambda error: _run_set_status(
                        run_id, "error", error, finished=True
                    ),
                )

                if cfg and not cfg.mcp_enabled:
                    rejection = tool_rejections.handle_mcp_disabled(
                        rejection_context, tool, arguments, call_id,
                        last_rejected_tool_sig, rejected_repeat,
                    )
                    last_rejected_tool_sig = rejection.signature
                    rejected_repeat = rejection.count
                    return rejection.action

                # Legacy compat: some text-protocol models glue several tool
                # names into one. Split and run them under this single call id.
                joined_native_tools = _split_concatenated_native_tool_name(tool, native_tool_name_map)
                if joined_native_tools:
                    joined_mcp_tools = [native_tool_name_map.get(item, item) for item in joined_native_tools]
                    joined_outcome = tool_persistence.execute_and_persist_joined_batch(
                        JoinedToolRequest(
                            tools=tuple(joined_mcp_tools),
                            arguments=arguments,
                            allowed_tools=frozenset(effective_tool_allowlist),
                            user_id=user_id,
                            ai_config_id=ai_config_id,
                        ),
                        tool_persistence.JoinedPersistenceContext(
                            session=bg, saved_message=saved,
                            user_id=user_id, ai_config_id=ai_config_id,
                            ai_kind=ai_kind, session_id=session_id,
                            session_name=session_name, model=model, run_id=run_id,
                            plan_active=plan_state is not None,
                            phase_mcp_statuses=phase_mcp_statuses,
                            should_stop=lambda: _run_should_stop(run_id),
                            mark_waiting=lambda name: _set_run_live_phase(
                                run_id,
                                "waiting_mcp",
                                name,
                            ),
                        ),
                    )
                    if joined_outcome.stopped:
                        _run_set_status(run_id, "stopped", finished=True)
                        return TurnCallAction.STOP_RUN
                    _append_joined_tool_response(
                        convo,
                        tool,
                        joined_outcome.items,
                        joined_outcome.failed,
                        call_id,
                        native=_has_native_tc,
                    )
                    return TurnCallAction.NEXT_CALL

                if tool not in effective_tool_allowlist:
                    rejection = tool_rejections.handle_disallowed_tool(
                        rejection_context, tool, arguments, call_id,
                        effective_tool_allowlist,
                        last_rejected_tool_sig, rejected_repeat,
                    )
                    last_rejected_tool_sig = rejection.signature
                    rejected_repeat = rejection.count
                    return rejection.action

                _set_run_live_phase(run_id, "waiting_mcp", tool)
                execution = _execute_tool_call(
                    tool,
                    user_id,
                    arguments,
                    ai_config_id,
                )
                tool_result = execution.result
                tool_failed = execution.failed
                tool_error = execution.error
                result_text = execution.display_text
                _record_mcp_call(tool_persistence.ToolCallRecord(
                    tool=tool, user_id=user_id, ai_config_id=ai_config_id,
                    session_id=session_id, run_id=run_id, message_id=getattr(saved, "id", None),
                    failed=tool_failed, error=tool_error,
                ))
                if plan_state is not None:
                    phase_context.record_status(phase_mcp_statuses, tool, tool_failed)
                renamed_session = tool_metadata.apply_tool_metadata(
                    tool_metadata.ToolMetadataContext(
                        session=bg, user_id=user_id, ai_config_id=ai_config_id,
                        ai_kind=ai_kind, session_id=session_id,
                        session_name=session_name,
                        allowed_tools=frozenset(effective_tool_allowlist),
                        exposed_tools=exposed_tool_allowlist,
                    ),
                    tool, tool_result, tool_failed,
                )
                session_name = tool_metadata.apply_session_rename(
                    saved, session_name, renamed_session
                )
                saved.tags = _append_mcp_state_to_tags(saved.tags, tool, arguments, result_text)
                bg.add(saved)
                bg.commit()
                screenshot_ref = {} if tool_failed else _screenshot_display_ref(tool, tool_result)
                _save_mcp_tool_bubble(tool_persistence.ToolBubbleRequest(
                    session=bg,
                    user_id=user_id,
                    ai_config_id=ai_config_id,
                    ai_kind=ai_kind,
                    session_id=session_id,
                    session_name=session_name,
                    model=model,
                    tool=tool,
                    arguments=arguments,
                    result_text=result_text,
                    failed=tool_failed,
                    image_url=screenshot_ref.get("url", ""),
                    image_data_url=screenshot_ref.get("data_url", ""),
                    tool_result=tool_result if isinstance(tool_result, dict) else None,
                    latency=execution.latency,
                ))

                transition = plan_transitions.handle_plan_transition(
                    plan_transitions.PlanTransitionContext(
                        session=bg,
                        conversation=convo,
                        pending=pending,
                        screenshot_messages=turn_screenshot_messages,
                        user_id=user_id,
                        ai_config_id=ai_config_id,
                        ai_kind=ai_kind,
                        session_id=session_id,
                        session_name=session_name,
                        model=model,
                        native_tool_calls=_has_native_tc,
                        system_prompt=system_prompt,
                        current_user_message_id=current_user_message_id,
                        model_user_content=model_user_content,
                        set_live_phase=lambda phase: _set_run_live_phase(run_id, phase),
                        complete_run=lambda: _run_set_status(
                            run_id,
                            "completed",
                            finished=True,
                        ),
                        auto_finalize_plan=_auto_finalize_plan,
                    ),
                    plan_transitions.PlanFlowSnapshot(
                        plan_state=plan_state,
                        awaiting_finish=flow_awaiting_finish,
                        phase_start_convo_index=phase_start_convo_index,
                        phase_started_at=phase_started_at,
                        phase_mcp_statuses=phase_mcp_statuses,
                    ),
                    plan_transitions.ControlToolCall(
                        tool=tool, arguments=arguments, tool_result=tool_result,
                        failed=tool_failed, call_id=call_id,
                    ),
                )
                if transition is not None:
                    plan_state = transition.snapshot.plan_state
                    flow_awaiting_finish = transition.snapshot.awaiting_finish
                    phase_start_convo_index = transition.snapshot.phase_start_convo_index
                    phase_started_at = transition.snapshot.phase_started_at
                    phase_mcp_statuses = transition.snapshot.phase_mcp_statuses
                    return transition.action

                # ---- ordinary tool: hand the result back and keep going --------
                _append_ordinary_tool_response(
                    ToolResponseContext(
                        conversation=convo,
                        screenshot_messages=turn_screenshot_messages,
                        turn_convo_start=turn_convo_start,
                        image_input_disabled=image_input_disabled,
                        native_tool_calls=_has_native_tc,
                    ),
                    tool,
                    arguments,
                    tool_result,
                    tool_failed,
                    call_id,
                )
                return TurnCallAction.NEXT_CALL

            # The configured step limit protects ordinary conversations from
            # runaway loops. Once a todo exists, completion (or an explicit
            # user stop) becomes the terminal condition: a model-side natural
            # stop simply starts another inference turn.
            completed_steps = 0
            while _can_start_inference_step(completed_steps, max_steps, plan_state):
                completed_steps += 1
                step_label = (
                    f"{completed_steps}/"
                    f"{'todo' if _has_active_todo_plan(plan_state) else max_steps}"
                )
                if _run_should_stop(run_id):
                    _run_set_status(run_id, "stopped", finished=True)
                    return

                pending_ai_reply_message_id = step_preparation.ingest_step_messages(
                    step_preparation.StepMessageContext(
                        session=bg, conversation=convo, user_id=user_id,
                        ai_config_id=ai_config_id, ai_kind=ai_kind,
                        session_id=session_id, session_name=session_name, model=model,
                    ),
                    pending_ai_reply_message_id,
                )

                _append_missing_tool_responses(
                    convo,
                    "Synthetic tool result inserted before request because the previous tool call did not receive a tool response.",
                )
                if image_input_disabled:
                    removed_images = _degrade_image_messages_to_text(convo)
                    if removed_images:
                        convo.append({
                            "role": "user",
                            "content": _image_input_degraded_feedback(
                                "The current model previously rejected image input in this run.",
                                removed_images,
                            ),
                        })
                exposure = step_preparation.select_tool_exposure(
                    step_preparation.ToolExposureRequest(
                        mcp_active=mcp_active,
                        exposed_tools=frozenset(exposed_tool_allowlist),
                        allowed_tools=frozenset(effective_tool_allowlist),
                        task_runtime=is_task_runtime,
                        plan_active=plan_state is not None,
                        awaiting_finish=flow_awaiting_finish,
                        tool_protocol=tool_protocol,
                    )
                )
                current_exposed_tools = set(exposure.current_tools)
                step_tools = exposure.provider_tools
                native_tool_name_map = exposure.native_name_map
                start_at = time.time()
                _ai_debug_stage(
                    "TURN",
                    f"{_ai_short_run_id(run_id)} #{step_label} "
                    f"start msgs={len(convo)} tools={len(step_tools)} "
                    f"reply={'y' if pending_ai_reply_message_id else 'n'}",
                    "33",
                )
                try:
                    if provider == "anthropic":
                        sr = stream_turn_anthropic(
                            run_id=run_id,
                            base_url=base_url,
                            api_key=api_key,
                            model=model,
                            convo=convo,
                            step_tools=step_tools,
                            native_tool_name_map=native_tool_name_map,
                        )
                    else:
                        oa_payload = {
                            "model": model,
                            "messages": convo,
                            "stream": True,
                            "stream_options": {"include_usage": True},
                        }
                        if step_tools:
                            oa_payload["tools"] = step_tools
                            oa_payload["tool_choice"] = "auto"
                            # The worker executes every tool call the model emits
                            # in a turn, answering each tool_call_id before the
                            # next request. Letting the model batch independent
                            # actions collapses N round trips into one.
                            oa_payload["parallel_tool_calls"] = True
                        response = ai_http_post(base_url, headers=headers, json=oa_payload, timeout=300, stream=True)
                        if not response.ok and "parallel_tool_calls" in oa_payload:
                            unsupported_hint = str(response.text or "").lower()
                            if "parallel_tool_calls" in unsupported_hint and (
                                "unsupported" in unsupported_hint
                                or "unknown" in unsupported_hint
                                or "invalid" in unsupported_hint
                                or "extra" in unsupported_hint
                            ):
                                oa_payload.pop("parallel_tool_calls", None)
                                response.close()
                                response = ai_http_post(base_url, headers=headers, json=oa_payload, timeout=300, stream=True)
                        _raise_for_upstream_error(response)
                        sr = stream_turn_openai_compat(
                            run_id=run_id,
                            response=response,
                            native_tool_name_map=native_tool_name_map,
                        )
                    consecutive_ai_errors = 0
                except Exception as ai_exc:
                    consecutive_ai_errors += 1
                    error_text = _extract_mcp_error(ai_exc)
                    _ai_debug_stage(
                        "ERR",
                        f"{_ai_short_run_id(run_id)} #{step_label} "
                        f"x{consecutive_ai_errors} {_ai_short(error_text, 140)}",
                        "31",
                    )
                    repaired_ids = _append_missing_tool_responses(convo, error_text)
                    if repaired_ids:
                        consecutive_ai_errors = 0
                        _save_message(
                            bg,
                            user_id,
                            ChatMessageCreate(
                                role="system",
                                content="\n".join([
                                    "[AI 对话上下文已修复]",
                                    "已补齐缺失的 tool 响应，避免上游接口因 tool_calls 上下文不完整而拒绝请求。",
                                    f"补齐 tool_call_id: {', '.join(repaired_ids)}",
                                ]),
                                tags="system_notice_ai_context_repaired",
                                ai_config_id=ai_config_id,
                                ai_kind=ai_kind,
                                session_id=session_id,
                                session_name=session_name,
                                model=model,
                                total_tokens=0,
                            ),
                        )
                        _set_run_live_phase(run_id, "generating")
                        continue
                    if _is_image_input_unsupported_error(error_text):
                        removed_images = _degrade_image_messages_to_text(convo)
                        if removed_images:
                            image_input_disabled = True
                            consecutive_ai_errors = 0
                            convo.append({
                                "role": "user",
                                "content": _image_input_degraded_feedback(error_text, removed_images),
                            })
                            notice = "\n".join([
                                "[AI 对话出错]",
                                error_text,
                                "",
                                f"检测到当前模型不支持图片输入；系统已移除 {removed_images} 张图片。",
                                "该错误已作为运行时消息发送给 AI，对话将继续执行。",
                            ])
                            _save_message(
                                bg,
                                user_id,
                                ChatMessageCreate(
                                    role="system",
                                    content=notice,
                                    tags="system_notice_ai_error",
                                    ai_config_id=ai_config_id,
                                    ai_kind=ai_kind,
                                    session_id=session_id,
                                    session_name=session_name,
                                    model=model,
                                    total_tokens=0,
                                ),
                            )
                            _set_run_live_phase(run_id, "generating")
                            continue
                    notice_lines = [
                        "[AI 对话出错]",
                        error_text,
                        "",
                        f"连续错误次数: {consecutive_ai_errors}/3",
                    ]
                    if consecutive_ai_errors < 3:
                        notice_lines.extend([
                            "",
                            "系统将重试上游请求；该错误不会作为 user 消息发送给 AI。",
                        ])
                    notice = "\n".join(notice_lines)
                    _save_message(
                        bg,
                        user_id,
                        ChatMessageCreate(
                            role="system",
                            content=notice,
                            tags="system_notice_ai_error",
                            ai_config_id=ai_config_id,
                            ai_kind=ai_kind,
                            session_id=session_id,
                            session_name=session_name,
                            model=model,
                            total_tokens=0,
                        ),
                    )
                    _set_run_live_phase(run_id, "generating")
                    if consecutive_ai_errors >= 3:
                        _run_set_status(run_id, "error", f"AI request failed 3 times consecutively: {error_text}", finished=True)
                        return
                    continue

                if sr.stopped:
                    _run_set_status(run_id, "stopped", finished=True)
                    return
                if _run_should_stop(run_id):
                    _run_set_status(run_id, "stopped", finished=True)
                    return

                assistant_text = sr.assistant_text
                reasoning_content = sr.reasoning_content
                usage = sr.usage
                finish_reason = sr.finish_reason
                _has_native_tc = sr.has_native_tc
                latency = time.time() - start_at

                # Every tool call this turn produced. The batch runs to completion
                # before the next inference step, so a model that plans several
                # independent actions pays one round trip instead of N.
                turn_calls: List[Dict[str, Any]] = []
                for _raw_call in sr.tool_calls:
                    _resolved_call = dict(_raw_call)
                    _resolved_call["tool"] = _resolve_mcp_tool_name(
                        _raw_call.get("tool", ""), native_tool_name_map, effective_tool_allowlist
                    )
                    turn_calls.append(_resolved_call)

                token_triplet = (
                    f"{int(usage.get('prompt_tokens') or 0)}/"
                    f"{int(usage.get('completion_tokens') or 0)}/"
                    f"{int(usage.get('total_tokens') or 0)}"
                )
                _ai_debug_stage(
                    "DONE",
                    f"{_ai_short_run_id(run_id)} #{step_label} "
                    f"{finish_reason or 'stop'} {int(latency * 1000)}ms tok={token_triplet} "
                    f"tc={'native:' if _has_native_tc else ''}"
                    f"{_ai_short(', '.join(c['tool'] for c in turn_calls) or '-', 48)}",
                    "32",
                )

                saved = _save_message(
                    bg,
                    user_id,
                    ChatMessageCreate(
                        role="assistant",
                        content=assistant_text,
                        think=reasoning_content or None,
                        tags="mcp_assistant_call" if turn_calls else "",
                        ai_config_id=ai_config_id,
                        ai_kind=ai_kind,
                        session_id=session_id,
                        session_name=session_name,
                        model=model,
                        prompt_tokens=int(usage.get("prompt_tokens") or 0),
                        completion_tokens=int(usage.get("completion_tokens") or 0),
                        total_tokens=int(usage.get("total_tokens") or 0),
                        cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0) or None,
                        system_prompt=system_prompt,
                        finish_reason=finish_reason,
                        latency=latency,
                    ),
                )

                # Where this turn starts in the live conversation. Screenshot
                # pruning is bounded by it, so two captures inside one batch both
                # survive while older ones still get dropped.
                turn_convo_start = len(convo)
                # Screenshot images captured by this turn's tools, held back until
                # every tool response is appended (see _flush_turn_screenshots).
                turn_screenshot_messages: List[Dict] = []
                if _has_native_tc and turn_calls:
                    assistant_item = {
                        "role": "assistant",
                        "content": assistant_text or None,
                        "tool_calls": [
                            {
                                "id": call["id"],
                                "type": "function",
                                "function": {
                                    "name": call["native_name"] or call["tool"],
                                    "arguments": call["raw_arguments"] or _safe_json(call["arguments"]),
                                },
                            }
                            for call in turn_calls
                        ],
                    }
                else:
                    assistant_item = {"role": "assistant", "content": assistant_text}
                if reasoning_content:
                    assistant_item["reasoning_content"] = reasoning_content
                convo.append(assistant_item)
                _set_run_live_text(run_id, "")
                _set_run_live_usage(run_id, 0, 0, 0)

                compression_context = compression_flow.CompressionContext(
                    session=bg, user=user, config=cfg, user_id=user_id,
                    ai_config_id=ai_config_id, ai_kind=ai_kind,
                    session_id=session_id, session_name=session_name, model=model,
                    api_key=api_key, base_url=base_url, system_prompt=system_prompt,
                    compression_prompt=str(auto_ctl.get("compression_prompt") or ""),
                    plan_state=plan_state,
                    reset_live_usage=lambda: _set_run_live_usage(run_id, 0, 0, 0),
                    set_generating=lambda: _set_run_live_phase(run_id, "generating"),
                    inject_flow_directive=_inject_flow_directive,
                )
                compression_state = compression_flow.CompressionState(
                    conversation=convo, compression_failed=compression_failed,
                    phase_start_convo_index=phase_start_convo_index,
                    phase_started_at=phase_started_at,
                    phase_mcp_statuses=phase_mcp_statuses,
                )
                compression = compression_flow.handle_manual_compression(
                    compression_context, compression_state, turn_calls, _has_native_tc
                )
                if compression.handled:
                    convo = compression.state.conversation
                    compression_failed = compression.state.compression_failed
                    phase_start_convo_index = compression.state.phase_start_convo_index
                    phase_started_at = compression.state.phase_started_at
                    phase_mcp_statuses = compression.state.phase_mcp_statuses
                    continue

                if is_task_runtime:
                    latest_task_job = _load_task_job_by_session(bg, user_id, ai_config_id, session_id)
                    if latest_task_job:
                        task_job = latest_task_job

                task_is_finished = bool(task_job and _is_task_finished_status(str(task_job.status or "")))
                compression = compression_flow.maybe_auto_compress(
                    compression_context,
                    compression.state,
                    turn_calls,
                    task_is_finished,
                )
                convo = compression.state.conversation
                compression_failed = compression.state.compression_failed
                phase_start_convo_index = compression.state.phase_start_convo_index
                phase_started_at = compression.state.phase_started_at
                phase_mcp_statuses = compression.state.phase_mcp_statuses
                if compression.continue_loop:
                    continue

                # Plan-mode gate: once a plan exists, reject a turn whose calls do
                # not move the current plan forward. The assistant tool_call message
                # was already appended above, so answer every id (native) / reply
                # (text) and steer back.
                if (
                    turn_calls
                    and is_task_runtime
                    and plan_state is not None
                    and any(not _flow_allowed_tool(call["tool"]) for call in turn_calls)
                ):
                    if flow_awaiting_finish:
                        _flow_block_text = phase_context.render_finish_required_notice(plan_state.goal)
                    else:
                        _flow_block_text = phase_context.render_continue_phase_notice()
                    if _has_native_tc:
                        _answer_pending_calls(
                            convo,
                            turn_calls,
                            {"success": False, "error": "flow_violation", "note": _flow_block_text},
                            native=True,
                        )
                    else:
                        convo.append({"role": "user", "content": _flow_block_text})
                    _set_run_live_phase(run_id, "generating")
                    continue

                if not turn_calls:
                    # Only check for text-format MCP warnings when not using native tool_calls.
                    if not _has_native_tc:
                        warning = _build_mcp_stream_warning(
                            assistant_text,
                            cfg,
                            mcp_warning_template,
                            markup_fallback=markup_fallback_available,
                        )
                        if warning:
                            markup_fallback_available = False
                            _save_message(
                                bg,
                                user_id,
                                ChatMessageCreate(
                                    role="user",
                                    content=warning,
                                    tags="system_notice_mcp_format_invalid",
                                    ai_config_id=ai_config_id,
                                    ai_kind=ai_kind,
                                    session_id=session_id,
                                    session_name=session_name,
                                    model=model,
                                    total_tokens=0,
                                ),
                            )
                            convo.append({"role": "user", "content": warning})
                            continue
                    # 收尾前最后一次排空：用户可能正是在 AI 输出这段最终回答时
                    # 插入了新消息。此时不要结束本轮，直接把消息接上继续处理，
                    # 兑现"一个深度思考/一次工具调用后就插入"的即时性。
                    try:
                        _final_injects = chat_inject.pop_pending_injects(
                            user_id, ai_config_id, ai_kind, session_id
                        )
                    except Exception:
                        _final_injects = []
                        logger.exception("final pending user-inject drain failed")
                    if _final_injects:
                        for _inject_text in _final_injects:
                            convo.append({"role": "user", "content": _inject_text})
                        _set_run_live_phase(run_id, "generating")
                        continue
                    if pending_ai_reply_message_id and assistant_text.strip() and ai_config_id is not None:
                        try:
                            _auto_reply = ai_message_service.complete_inbound_with_assistant_reply(
                                message_id=pending_ai_reply_message_id,
                                user_id=user_id,
                                replier_ai_config_id=int(ai_config_id),
                                content=assistant_text,
                            )
                            if _auto_reply and _auto_reply.get("auto_completed"):
                                saved.tags = _append_mcp_state_to_tags(
                                    saved.tags,
                                    "ai.auto_reply",
                                    {"message_id": pending_ai_reply_message_id},
                                    "assistant final text delivered as AI message reply",
                                )
                                bg.add(saved)
                                bg.commit()
                        except Exception:
                            logger.exception("auto AI message reply failed")
                        finally:
                            pending_ai_reply_message_id = ""
                    # A model-side natural stop must not terminate an unfinished
                    # todo. Refresh from DB in case another process changed the
                    # plan, then invoke the model again without appending any
                    # user/system nudge to the conversation.
                    if ai_config_id is not None:
                        try:
                            plan_state = plan_service.get_active_plan(
                                bg, user_id, int(ai_config_id), session_id
                            )
                            flow_awaiting_finish = plan_service.awaiting_finish(bg, plan_state)
                        except Exception:
                            logger.exception("plan reload after natural model stop failed")
                    if _has_active_todo_plan(plan_state):
                        if flow_awaiting_finish:
                            _auto_finalize_plan(phase_started_at)
                            _set_run_live_phase(run_id, "idle")
                            _run_set_status(run_id, "completed", finished=True)
                            return
                        _set_run_live_phase(run_id, "generating")
                        continue
                    # Auto-finalize simple (non-plan) task jobs when the run ends naturally.
                    # If a todo plan exists, the AI must keep working until its current
                    # phase is updated through todo.manage(action=edit).
                    # (enforced by flow_awaiting_finish + _flow_allowed_tool).
                    if task_job is not None and str(getattr(task_job, "status", "") or "").strip() not in {"completed", "cancelled", "stopped", "error"}:
                        try:
                            active_plan = None
                            if ai_config_id is not None:
                                active_plan = plan_service.get_active_plan(bg, user_id, int(ai_config_id), session_id)
                            if active_plan is None:
                                finished_at = time.time()
                                try:
                                    _notify_task_completion(
                                        user_id=user_id,
                                        job_id=str(task_job.job_id or ""),
                                        summary="任务执行完成（简单任务，无计划流程）。",
                                    )
                                except Exception:
                                    logger.exception("auto simple task completion notify failed")
                                # 循环任务原地续期（同一 job 回到 queued 等待下一轮）；
                                # 未续期（非循环 / 循环已结束）才真正标记完成。
                                _renewed_loop_job = None
                                try:
                                    _renewed_loop_job = _renew_loop_scheduled_job(bg, task_job, finished_at)
                                except Exception:
                                    logger.exception("auto simple task loop schedule failed")
                                if _renewed_loop_job is None:
                                    task_job.status = "completed"
                                    task_job.finished_at = finished_at
                                    task_job.updated_at = finished_at
                                    bg.add(task_job)
                                try:
                                    bg.commit()
                                except Exception:
                                    pass
                        except Exception:
                            logger.exception("auto finalize simple task job failed")
                    _run_set_status(run_id, "completed", finished=True)
                    return

                # ---- cross-step no-progress guard -----------------------------
                # 模型（尤其 grok 经无状态网关驱动时）有概率连续多步原样重放上一步
                # 的整批工具调用：同样的查询、同样的参数，却期待不同结果。相同调用不
                # 会有不同结果，继续执行只会重复副作用、把剩余步数全烧在原地。用批次
                # 签名（工具名+参数，忽略顺序）识别连续重复：命中就不再执行本批，改为
                # 强提示打断；仍不改则收尾，避免死循环。
                _batch_sig = "\n".join(sorted(
                    f"{c.get('tool') or ''}|"
                    f"{json.dumps(c.get('arguments') or {}, ensure_ascii=False, sort_keys=True)}"
                    for c in turn_calls
                ))
                if _batch_sig and _batch_sig == last_batch_sig:
                    consecutive_same_batch += 1
                else:
                    consecutive_same_batch = 1
                last_batch_sig = _batch_sig
                if consecutive_same_batch >= 2:
                    _ai_debug_stage(
                        "LOOP",
                        f"{_ai_short_run_id(run_id)} #{step_label} x{consecutive_same_batch} "
                        f"{_ai_short(', '.join(c['tool'] for c in turn_calls), 48)}",
                        "31",
                    )
                    _loop_note = (
                        "[系统提示] 检测到你连续多步发出了完全相同的工具调用（相同工具名与参数），"
                        "但没有产生新进展。相同的调用不会返回不同的结果——请直接基于上方已有的"
                        "工具结果继续推进，或明确给出结论，不要再重复相同的调用与相同的思考。"
                    )
                    if _has_native_tc:
                        _answer_pending_calls(
                            convo,
                            turn_calls,
                            {"success": False, "error": "no_progress_loop", "note": _loop_note},
                            native=True,
                        )
                    else:
                        convo.append({"role": "user", "content": _loop_note})
                    if consecutive_same_batch >= 3:
                        # 已提示过仍在原地重放：收尾，避免把剩余步数全烧在同一循环上。
                        _save_message(
                            bg,
                            user_id,
                            ChatMessageCreate(
                                role="system",
                                content=(
                                    "[系统提示]\n"
                                    "检测到连续多步重复相同的工具调用且无新进展，已自动结束本轮以避免死循环。"
                                    "如需继续，请发送新消息或调整需求。"
                                ),
                                tags="system_notice_no_progress_loop",
                                ai_config_id=ai_config_id,
                                ai_kind=ai_kind,
                                session_id=session_id,
                                session_name=session_name,
                                model=model,
                                total_tokens=0,
                            ),
                        )
                        _set_run_live_phase(run_id, "idle")
                        _run_set_status(run_id, "completed", finished=True)
                        return
                    _set_run_live_phase(run_id, "generating")
                    continue

                # ---- run this turn's tool calls as a batch --------------------
                # Each call is answered before the next request goes out. A
                # control-flow tool ends the batch early and closes out the
                # remaining ids itself.
                #
                # 同一轮里模型偶尔会一口气发出多个完全相同（同工具名 + 同参数）的
                # 调用。批处理会逐个执行，对有副作用的工具（发消息、点击、提交）就
                # 是重复动作。这里对本轮内的精确重复只执行第一次，其余直接以“已合并”
                # 结果答复——既保住原生 tool_call_id 必须逐个答复的契约，又避免重复副作用。
                batch_action = TurnCallAction.NEXT_CALL
                _dup_flags = _duplicate_call_flags(turn_calls)
                for call_index, turn_call in enumerate(turn_calls):
                    if _dup_flags[call_index]:
                        _ai_debug_stage(
                            "DEDUP",
                            f"{_ai_short_run_id(run_id)} #{step_label} "
                            f"{_ai_short(str(turn_call.get('tool') or '?'), 40)}",
                            "33",
                        )
                        _answer_pending_calls(
                            convo,
                            [turn_call],
                            {
                                "success": True,
                                "note": "duplicate_call_merged",
                                "detail": "本轮已执行过完全相同的工具调用（同名同参数），此重复调用未再次执行，结果同上。",
                            },
                            native=_has_native_tc,
                        )
                        continue
                    batch_action = _execute_turn_call(turn_call, turn_calls[call_index + 1:])
                    if batch_action is not TurnCallAction.NEXT_CALL:
                        break
                if batch_action is TurnCallAction.STOP_RUN:
                    return
                if batch_action is TurnCallAction.NEXT_TURN:
                    # A barrier already flushed or dropped the held screenshots.
                    continue
                # Batch drained: every tool_call_id is answered, so the screenshot
                # images can now follow the tool messages without splitting them.
                _flush_screenshot_messages(convo, turn_screenshot_messages)
                _set_run_live_phase(run_id, "generating")

            notice = (
                "[系统提示]\n"
                f"本轮已达到 MCP 连续执行步数上限（{max_steps}）。"
                "系统已暂停本轮自动继续，避免无限循环；如需继续，请发送新消息或提高系统设置里的 MCP 最大步数。"
            )
            _save_message(
                bg,
                user_id,
                ChatMessageCreate(
                    role="system",
                    content=notice,
                    tags="system_notice_mcp_max_steps",
                    ai_config_id=ai_config_id,
                    ai_kind=ai_kind,
                    session_id=session_id,
                    session_name=session_name,
                    model=model,
                    total_tokens=0,
                ),
            )
            _run_set_status(run_id, "completed", finished=True)
    except Exception as exc:
        _run_set_status(run_id, "error", str(exc), finished=True)
