IS_ROUTER_ENTRY = False

import asyncio
import logging
import time


logger = logging.getLogger(__name__)
from typing import Any, Dict, List, Optional

from sqlmodel import Session

from api.database import engine
from api.runtime import run_context
from mcp_runtime.mcp import get_project_root
from api.models import ChatMessageCreate
from api.services.tasks import task_plan as plan_service
from ai_runtime.inference import model_gateway
from ai_runtime.inference import phase_context
from ai_runtime.inference import plan_transitions
from ai_runtime.inference import tool_media
from ai_runtime.inference import tool_batch_flow
from ai_runtime.inference import turn_call_flow
from ai_runtime.inference import worker_lifecycle
from ai_runtime.inference import worker_post_turn_flow
from ai_runtime.inference import worker_setup
from ai_runtime.inference import worker_turn_flow
from ai_runtime.inference.debug_support import (
    ai_color as _ai_color,
    ai_debug_enabled as _ai_debug_enabled,
    ai_debug_log as _ai_debug_log,
    ai_debug_stage as _ai_debug_stage,
    ai_short as _ai_short,
    ai_short_base_url as _ai_short_base_url,
    ai_short_run_id as _ai_short_run_id,
)
from ai_runtime.inference.policies import (
    can_start_inference_step as _can_start_inference_step,
    has_active_todo_plan as _has_active_todo_plan,
)
from ai_runtime.inference.plan_flow import (
    PlanFinalizeContext,
    append_plan_directive,
    finalize_plan,
)
from ai_runtime.inference.tool_resolution import (
    TurnCallAction,
    flush_screenshot_messages as _flush_screenshot_messages,
    resolve_mcp_tool_name as _resolve_mcp_tool_name,
)
from ai_runtime.inference.run_request import WorkerRequest, start_worker_run
get_run_session_context = run_context.get_run_session_context
set_run_session_context = run_context.set_run_session_context
from api.chat_runtime.chat_prompt_utils import (
    _append_prompt_section,
    _filter_tools_for_current_bindings,
    _set_run_live_phase,
    _set_run_live_text,
    _set_run_live_usage,
)
from api.services.chat.chat_persistence import _save_message
from api.chat_runtime.chat_runtime_helpers import (
    _parse_allowed_tools,
    _run_set_status,
    _run_should_stop,
)


_duplicate_call_flags = tool_batch_flow.duplicate_call_flags
_raise_for_upstream_error = model_gateway.raise_for_upstream_error
_is_image_input_unsupported_error = tool_media.is_image_input_unsupported_error
_prune_prior_runtime_screenshot_images = tool_media.prune_prior_runtime_screenshot_images
_find_image_payload = tool_media.find_image_payload
_image_path_to_data_url = tool_media.image_path_to_data_url
_omit_image_fields = tool_media.omit_image_fields
_tool_image_message = tool_media.tool_image_message
_browser_screenshot_image_message = tool_media.tool_image_message
_find_screenshot_result_payload = tool_media.find_screenshot_result_payload
_screenshot_send_to_user_enabled = tool_media.screenshot_send_to_user_enabled
_model_visible_tool_result = tool_media.model_visible_tool_result


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
    worker_lifecycle.run_worker(
        WorkerRequest.create(
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
        ),
        _run_worker_impl,
    )


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
            setup = worker_setup.prepare_worker(bg, request)
            max_steps = setup.max_steps
            cfg = setup.config
            api_key = setup.api_key
            base_url = setup.base_url
            model = setup.model
            system_prompt = setup.system_prompt
            task_job = setup.task_job
            is_task_runtime = setup.is_task_runtime
            effective_tool_allowlist = setup.effective_tool_allowlist
            history = setup.history
            convo = setup.conversation
            markup_fallback_available = True
            compression_failed = False

            capabilities = worker_setup.prepare_capabilities(bg, request, setup)
            headers = capabilities.headers
            last_rejected_tool_sig = ""
            rejected_repeat = 0
            consecutive_ai_errors = 0
            image_input_disabled = False
            # 跨步「原地重放」检测：连续若干步产出完全相同的工具批次（同名同参数）
            # 说明模型卡住了——同一查询不会给出不同结果，继续执行只是空烧步数。
            last_batch_sig = ""
            consecutive_same_batch = 0

            mcp_active = capabilities.mcp_active
            exposed_tool_allowlist = set(capabilities.exposed_tool_allowlist)
            provider = capabilities.provider
            tool_protocol = capabilities.tool_protocol
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

                worker_turn = worker_turn_flow.run_worker_turn(
                    worker_turn_flow.WorkerTurnContext(
                        session=bg,
                        conversation=convo,
                        user_id=user_id,
                        ai_config_id=ai_config_id,
                        ai_kind=ai_kind,
                        session_id=session_id,
                        model=model,
                        system_prompt=system_prompt,
                        run_id=run_id,
                        provider=provider,
                        base_url=base_url,
                        api_key=api_key,
                        headers=headers,
                        should_stop=lambda: _run_should_stop(run_id),
                        stop_run=lambda: _run_set_status(
                            run_id, "stopped", finished=True
                        ),
                        set_live_phase=lambda phase: _set_run_live_phase(
                            run_id, phase
                        ),
                        set_run_error=lambda error: _run_set_status(
                            run_id, "error", error, finished=True
                        ),
                        clear_live_text=lambda: _set_run_live_text(run_id, ""),
                        reset_live_usage=lambda: _set_run_live_usage(
                            run_id, 0, 0, 0
                        ),
                    ),
                    worker_turn_flow.WorkerTurnRequest(
                        step_label=step_label,
                        session_name=session_name,
                        state=worker_turn_flow.WorkerTurnState(
                            pending_reply_message_id=pending_ai_reply_message_id,
                            consecutive_errors=consecutive_ai_errors,
                            image_input_disabled=image_input_disabled,
                        ),
                        policy=worker_turn_flow.WorkerTurnPolicy(
                            mcp_active=mcp_active,
                            exposed_tools=frozenset(exposed_tool_allowlist),
                            allowed_tools=frozenset(effective_tool_allowlist),
                            task_runtime=is_task_runtime,
                            plan_active=plan_state is not None,
                            awaiting_finish=flow_awaiting_finish,
                            tool_protocol=tool_protocol,
                        ),
                    ),
                )
                pending_ai_reply_message_id = (
                    worker_turn.state.pending_reply_message_id
                )
                consecutive_ai_errors = worker_turn.state.consecutive_errors
                image_input_disabled = worker_turn.state.image_input_disabled
                if worker_turn.action is worker_turn_flow.WorkerTurnAction.STOP_RUN:
                    return
                if worker_turn.action is worker_turn_flow.WorkerTurnAction.RETRY:
                    continue
                persisted_turn = worker_turn.persisted_turn
                if persisted_turn is None:
                    raise RuntimeError("model turn proceeded without persistence")
                assistant_text = worker_turn.assistant_text
                _has_native_tc = worker_turn.native_tool_calls
                native_tool_name_map = worker_turn.native_tool_name_map or {}
                turn_calls = persisted_turn.tool_calls
                saved = persisted_turn.saved_message
                turn_convo_start = persisted_turn.conversation_start
                # Screenshot images captured by this turn's tools, held back until
                # every tool response is appended (see _flush_turn_screenshots).
                turn_screenshot_messages: List[Dict] = []

                post_turn = worker_post_turn_flow.handle_post_turn(
                    worker_post_turn_flow.PostTurnContext(
                        session=bg,
                        request=request,
                        setup=setup,
                        reset_live_usage=lambda: _set_run_live_usage(
                            run_id, 0, 0, 0
                        ),
                        set_live_phase=lambda phase: _set_run_live_phase(
                            run_id, phase
                        ),
                        inject_flow_directive=_inject_flow_directive,
                        auto_finalize_plan=_auto_finalize_plan,
                    ),
                    worker_post_turn_flow.PostTurnState(
                        conversation=convo,
                        session_name=session_name,
                        plan_state=plan_state,
                        awaiting_finish=flow_awaiting_finish,
                        phase_start_convo_index=phase_start_convo_index,
                        phase_started_at=phase_started_at,
                        phase_mcp_statuses=phase_mcp_statuses,
                        compression_failed=compression_failed,
                        task_job=task_job,
                        markup_fallback_available=markup_fallback_available,
                        pending_reply_message_id=pending_ai_reply_message_id,
                    ),
                    worker_post_turn_flow.PostTurnData(
                        saved_message=saved,
                        assistant_text=assistant_text,
                        native_tool_calls=_has_native_tc,
                        turn_calls=turn_calls,
                    ),
                )
                convo = post_turn.state.conversation
                plan_state = post_turn.state.plan_state
                flow_awaiting_finish = post_turn.state.awaiting_finish
                phase_start_convo_index = post_turn.state.phase_start_convo_index
                phase_started_at = post_turn.state.phase_started_at
                phase_mcp_statuses = post_turn.state.phase_mcp_statuses
                compression_failed = post_turn.state.compression_failed
                task_job = post_turn.state.task_job
                markup_fallback_available = (
                    post_turn.state.markup_fallback_available
                )
                pending_ai_reply_message_id = (
                    post_turn.state.pending_reply_message_id
                )
                if post_turn.action is worker_post_turn_flow.PostTurnAction.NEXT_TURN:
                    continue
                if post_turn.action is worker_post_turn_flow.PostTurnAction.COMPLETE_RUN:
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

                call_machine = turn_call_flow.TurnCallMachine(
                    turn_call_flow.TurnCallContext(
                        session=bg,
                        conversation=convo,
                        screenshot_messages=turn_screenshot_messages,
                        saved_message=saved,
                        user_id=user_id,
                        ai_config_id=ai_config_id,
                        ai_kind=ai_kind,
                        session_id=session_id,
                        model=model,
                        run_id=run_id,
                        config=cfg,
                        effective_tools=frozenset(effective_tool_allowlist),
                        native_tool_name_map=native_tool_name_map,
                        native_tool_calls=_has_native_tc,
                        system_prompt=system_prompt,
                        current_user_message_id=current_user_message_id,
                        model_user_content=model_user_content,
                        turn_conversation_start=turn_convo_start,
                        image_input_disabled=image_input_disabled,
                        should_stop=lambda: _run_should_stop(run_id),
                        stop_run=lambda: _run_set_status(
                            run_id, "stopped", finished=True
                        ),
                        complete_run=lambda: _run_set_status(
                            run_id, "completed", finished=True
                        ),
                        set_live_phase=lambda phase, tool="": _set_run_live_phase(
                            run_id, phase, tool
                        ),
                        set_run_error=lambda error: _run_set_status(
                            run_id, "error", error, finished=True
                        ),
                        auto_finalize_plan=_auto_finalize_plan,
                    ),
                    turn_call_flow.TurnCallState(
                        session_name=session_name,
                        exposed_tools=frozenset(exposed_tool_allowlist),
                        rejected_tool_signature=last_rejected_tool_sig,
                        rejected_repeat=rejected_repeat,
                        plan=plan_transitions.PlanFlowSnapshot(
                            plan_state=plan_state,
                            awaiting_finish=flow_awaiting_finish,
                            phase_start_convo_index=phase_start_convo_index,
                            phase_started_at=phase_started_at,
                            phase_mcp_statuses=phase_mcp_statuses,
                        ),
                    ),
                )
                batch_action = tool_batch_flow.execute_turn_batch(
                    convo,
                    turn_calls,
                    _has_native_tc,
                    call_machine.execute,
                    _debug_duplicate,
                )
                session_name = call_machine.state.session_name
                exposed_tool_allowlist = set(call_machine.state.exposed_tools)
                last_rejected_tool_sig = call_machine.state.rejected_tool_signature
                rejected_repeat = call_machine.state.rejected_repeat
                plan_state = call_machine.state.plan.plan_state
                flow_awaiting_finish = call_machine.state.plan.awaiting_finish
                phase_start_convo_index = call_machine.state.plan.phase_start_convo_index
                phase_started_at = call_machine.state.plan.phase_started_at
                phase_mcp_statuses = call_machine.state.plan.phase_mcp_statuses
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
