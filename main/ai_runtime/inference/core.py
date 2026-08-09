IS_ROUTER_ENTRY = False

import asyncio
import logging
import time


logger = logging.getLogger(__name__)
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from api.database import engine
from api.runtime import run_context
from mcp_runtime.mcp import get_project_root
from mcp_runtime.mcp.core import MCP_INTROSPECTION_TOOLS
from api.models import AITaskJob, ChatMessage, ChatMessageCreate, User
from api.services.chat import chat_inject, mcp_session_context
from api.services.tasks import task_plan as plan_service
from ai_runtime.inference import compression_flow
from ai_runtime.inference import final_response_flow
from ai_runtime.inference import model_error_flow
from ai_runtime.inference import model_gateway
from ai_runtime.inference import phase_context
from ai_runtime.inference import plan_transitions
from ai_runtime.inference import step_preparation
from ai_runtime.inference import tool_media
from ai_runtime.inference import tool_metadata
from ai_runtime.inference import tool_persistence
from ai_runtime.inference import tool_rejections
from ai_runtime.inference import tool_batch_flow
from ai_runtime.inference import turn_result
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
    _extract_mcp_error,
    _filter_tools_for_current_bindings,
    _set_run_live_meta,
    _set_run_live_phase,
    _set_run_live_text,
    _set_run_live_usage,
    _strip_prompt_section,
    _strip_task_runtime_sections,
)
from api.services.chat.chat_persistence import _save_message
from api.chat_runtime.chat_stream import _detect_provider
from api.chat_runtime.chat_runtime_helpers import (
    _is_task_finished_status,
    _load_task_job_by_session,
    _load_task_payload_by_session,
    _parse_allowed_tools,
    _resolve_ai_runtime,
    _run_set_status,
    _run_should_stop,
    build_runtime_system_prompt_and_tools,
)

from api.core.config import DEFAULT_CHAT_MAX_STEPS
from api.core.settings import settings


def _record_mcp_call(record: tool_persistence.ToolCallRecord) -> None:
    tool_persistence.record_tool_call(record)


_duplicate_call_flags = tool_batch_flow.duplicate_call_flags


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

                model_error_flow.repair_missing_tool_responses(
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
                    sr = model_gateway.run_model_turn(model_gateway.ModelTurnRequest(
                        run_id=run_id, provider=provider, base_url=base_url,
                        api_key=api_key, model=model, conversation=convo,
                        provider_tools=step_tools,
                        native_name_map=native_tool_name_map,
                        headers=headers,
                    ))
                    consecutive_ai_errors = 0
                except Exception as ai_exc:
                    error_text = _extract_mcp_error(ai_exc)
                    _ai_debug_stage(
                        "ERR",
                        f"{_ai_short_run_id(run_id)} #{step_label} "
                        f"x{consecutive_ai_errors + 1} {_ai_short(error_text, 140)}",
                        "31",
                    )
                    error_decision = model_error_flow.handle_model_error(
                        model_error_flow.ModelErrorContext(
                            session=bg, conversation=convo, user_id=user_id,
                            ai_config_id=ai_config_id, ai_kind=ai_kind,
                            session_id=session_id, session_name=session_name,
                            model=model,
                            set_generating=lambda: _set_run_live_phase(
                                run_id, "generating"
                            ),
                            set_run_error=lambda error: _run_set_status(
                                run_id, "error", error, finished=True
                            ),
                        ),
                        error_text,
                        consecutive_ai_errors,
                        image_input_disabled,
                    )
                    consecutive_ai_errors = error_decision.consecutive_errors
                    image_input_disabled = error_decision.image_input_disabled
                    if error_decision.stop_run:
                        return
                    continue

                if sr.stopped:
                    _run_set_status(run_id, "stopped", finished=True)
                    return
                if _run_should_stop(run_id):
                    _run_set_status(run_id, "stopped", finished=True)
                    return

                assistant_text = sr.assistant_text
                _has_native_tc = sr.has_native_tc
                latency = time.time() - start_at
                persisted_turn = turn_result.persist_assistant_turn(
                    turn_result.AssistantTurnContext(
                        session=bg, conversation=convo, user_id=user_id,
                        ai_config_id=ai_config_id, ai_kind=ai_kind,
                        session_id=session_id, session_name=session_name,
                        model=model, system_prompt=system_prompt,
                        native_tool_name_map=native_tool_name_map,
                        allowed_tools=frozenset(effective_tool_allowlist),
                    ),
                    sr,
                    latency,
                )
                turn_calls = persisted_turn.tool_calls
                saved = persisted_turn.saved_message
                turn_convo_start = persisted_turn.conversation_start
                _ai_debug_stage(
                    "DONE",
                    f"{_ai_short_run_id(run_id)} #{step_label} "
                    f"{sr.finish_reason or 'stop'} {int(latency * 1000)}ms "
                    f"tok={persisted_turn.token_triplet} "
                    f"tc={'native:' if _has_native_tc else ''}"
                    f"{_ai_short(', '.join(c['tool'] for c in turn_calls) or '-', 48)}",
                    "32",
                )
                # Screenshot images captured by this turn's tools, held back until
                # every tool response is appended (see _flush_turn_screenshots).
                turn_screenshot_messages: List[Dict] = []
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
                    final_response = final_response_flow.handle_final_response(
                        final_response_flow.FinalResponseContext(
                            session=bg, conversation=convo, saved_message=saved,
                            user_id=user_id, ai_config_id=ai_config_id,
                            ai_kind=ai_kind, session_id=session_id,
                            session_name=session_name, model=model, config=cfg,
                            warning_template=mcp_warning_template,
                            assistant_text=assistant_text,
                            native_tool_calls=_has_native_tc,
                            phase_started_at=phase_started_at,
                            set_live_phase=lambda phase: _set_run_live_phase(run_id, phase),
                            auto_finalize_plan=_auto_finalize_plan,
                            notify_task_completion=_notify_task_completion,
                        ),
                        final_response_flow.FinalResponseState(
                            markup_fallback_available=markup_fallback_available,
                            pending_ai_reply_message_id=pending_ai_reply_message_id,
                            plan_state=plan_state,
                            awaiting_finish=flow_awaiting_finish,
                            task_job=task_job,
                        ),
                    )
                    markup_fallback_available = final_response.state.markup_fallback_available
                    pending_ai_reply_message_id = final_response.state.pending_ai_reply_message_id
                    plan_state = final_response.state.plan_state
                    flow_awaiting_finish = final_response.state.awaiting_finish
                    if final_response.action is final_response_flow.FinalResponseAction.NEXT_TURN:
                        continue
                    _run_set_status(run_id, "completed", finished=True)
                    return

                progress = tool_batch_flow.evaluate_progress(
                    tool_batch_flow.ProgressContext(
                        session=bg, conversation=convo, user_id=user_id,
                        ai_config_id=ai_config_id, ai_kind=ai_kind,
                        session_id=session_id, session_name=session_name,
                        model=model, native_tool_calls=_has_native_tc,
                        set_live_phase=lambda phase: _set_run_live_phase(run_id, phase),
                    ),
                    tool_batch_flow.ProgressState(
                        last_batch_signature=last_batch_sig,
                        consecutive_same_batch=consecutive_same_batch,
                    ),
                    turn_calls,
                )
                last_batch_sig = progress.state.last_batch_signature
                consecutive_same_batch = progress.state.consecutive_same_batch
                if progress.action is not tool_batch_flow.ProgressAction.EXECUTE_BATCH:
                    _ai_debug_stage(
                        "LOOP",
                        f"{_ai_short_run_id(run_id)} #{step_label} x{consecutive_same_batch} "
                        f"{_ai_short(', '.join(c['tool'] for c in turn_calls), 48)}",
                        "31",
                    )
                    if progress.action is tool_batch_flow.ProgressAction.STOP_RUN:
                        _run_set_status(run_id, "completed", finished=True)
                        return
                    continue

                def _debug_duplicate(turn_call):
                        _ai_debug_stage(
                            "DEDUP",
                            f"{_ai_short_run_id(run_id)} #{step_label} "
                            f"{_ai_short(str(turn_call.get('tool') or '?'), 40)}",
                            "33",
                        )

                batch_action = tool_batch_flow.execute_turn_batch(
                    convo,
                    turn_calls,
                    _has_native_tc,
                    _execute_turn_call,
                    _debug_duplicate,
                )
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
