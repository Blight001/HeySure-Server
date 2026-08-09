"""Compression, plan gating and final-response decisions after a model turn."""

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Callable, Dict, List

from sqlmodel import Session

from api.chat_runtime.chat_runtime_helpers import (
    _is_task_finished_status,
    _load_task_job_by_session,
)
from ai_runtime.inference import compression_flow, final_response_flow, phase_context
from ai_runtime.inference.plan_flow import send_task_completion_notification
from ai_runtime.inference.run_request import WorkerRequest
from ai_runtime.inference.tool_resolution import append_pending_call_responses
from ai_runtime.inference.worker_setup import WorkerSetup
from mcp_runtime.mcp.core import MCP_INTROSPECTION_TOOLS


class PostTurnAction(Enum):
    EXECUTE_TOOLS = "execute_tools"
    NEXT_TURN = "next_turn"
    COMPLETE_RUN = "complete_run"


@dataclass(frozen=True)
class PostTurnContext:
    session: Session
    request: WorkerRequest
    setup: WorkerSetup
    reset_live_usage: Callable[[], None]
    set_live_phase: Callable[[str], None]
    inject_flow_directive: Callable[[List[Dict]], None]
    auto_finalize_plan: Callable[[float], None]


@dataclass(frozen=True)
class PostTurnState:
    conversation: List[Dict]
    session_name: str
    plan_state: Any
    awaiting_finish: bool
    phase_start_convo_index: int
    phase_started_at: float
    phase_mcp_statuses: List[tuple]
    compression_failed: bool
    task_job: Any
    markup_fallback_available: bool
    pending_reply_message_id: str


@dataclass(frozen=True)
class PostTurnData:
    saved_message: Any
    assistant_text: str
    native_tool_calls: bool
    turn_calls: List[Dict]


@dataclass(frozen=True)
class PostTurnOutcome:
    action: PostTurnAction
    state: PostTurnState


def handle_post_turn(
    context: PostTurnContext,
    state: PostTurnState,
    turn: PostTurnData,
) -> PostTurnOutcome:
    state, continue_loop = _handle_compression(context, state, turn)
    if continue_loop:
        return PostTurnOutcome(PostTurnAction.NEXT_TURN, state)
    if _flow_violation(context, state, turn):
        _append_flow_violation(context, state, turn)
        return PostTurnOutcome(PostTurnAction.NEXT_TURN, state)
    if turn.turn_calls:
        return PostTurnOutcome(PostTurnAction.EXECUTE_TOOLS, state)
    return _handle_final_response(context, state, turn)


def _handle_compression(context, state, turn):
    request = context.request
    setup = context.setup
    compression_context = compression_flow.CompressionContext(
        session=context.session,
        user=setup.user,
        config=setup.config,
        user_id=request.user_id,
        ai_config_id=request.ai_config_id,
        ai_kind=request.ai_kind,
        session_id=request.session_id,
        session_name=state.session_name,
        model=setup.model,
        api_key=setup.api_key,
        base_url=setup.base_url,
        system_prompt=setup.system_prompt,
        compression_prompt=str(
            setup.auto_control.get("compression_prompt") or ""
        ),
        plan_state=state.plan_state,
        reset_live_usage=context.reset_live_usage,
        set_generating=lambda: context.set_live_phase("generating"),
        inject_flow_directive=context.inject_flow_directive,
    )
    compression_state = compression_flow.CompressionState(
        conversation=state.conversation,
        compression_failed=state.compression_failed,
        phase_start_convo_index=state.phase_start_convo_index,
        phase_started_at=state.phase_started_at,
        phase_mcp_statuses=state.phase_mcp_statuses,
    )
    outcome = compression_flow.handle_manual_compression(
        compression_context,
        compression_state,
        turn.turn_calls,
        turn.native_tool_calls,
    )
    state = _apply_compression_state(state, outcome.state)
    if outcome.handled:
        return state, True
    state = _refresh_task_job(context, state)
    task_finished = bool(
        state.task_job
        and _is_task_finished_status(str(state.task_job.status or ""))
    )
    outcome = compression_flow.maybe_auto_compress(
        compression_context,
        outcome.state,
        turn.turn_calls,
        task_finished,
    )
    return _apply_compression_state(state, outcome.state), outcome.continue_loop


def _apply_compression_state(state, compressed) -> PostTurnState:
    return replace(
        state,
        conversation=compressed.conversation,
        compression_failed=compressed.compression_failed,
        phase_start_convo_index=compressed.phase_start_convo_index,
        phase_started_at=compressed.phase_started_at,
        phase_mcp_statuses=compressed.phase_mcp_statuses,
    )


def _refresh_task_job(context, state) -> PostTurnState:
    if not context.setup.is_task_runtime:
        return state
    request = context.request
    latest = _load_task_job_by_session(
        context.session,
        request.user_id,
        request.ai_config_id,
        request.session_id,
    )
    return replace(state, task_job=latest or state.task_job)


def _flow_violation(context, state, turn) -> bool:
    if (
        not turn.turn_calls
        or not context.setup.is_task_runtime
        or state.plan_state is None
    ):
        return False
    return any(
        not _flow_allowed_tool(call.get("tool"), state.awaiting_finish)
        for call in turn.turn_calls
    )


def _flow_allowed_tool(tool_name: str, awaiting_finish: bool) -> bool:
    name = str(tool_name or "")
    if name in MCP_INTROSPECTION_TOOLS:
        return True
    return not awaiting_finish or name == "todo.manage"


def _append_flow_violation(context, state, turn) -> None:
    text = (
        phase_context.render_finish_required_notice(state.plan_state.goal)
        if state.awaiting_finish
        else phase_context.render_continue_phase_notice()
    )
    if turn.native_tool_calls:
        append_pending_call_responses(
            state.conversation,
            turn.turn_calls,
            {"success": False, "error": "flow_violation", "note": text},
            native=True,
        )
    else:
        state.conversation.append({"role": "user", "content": text})
    context.set_live_phase("generating")


def _handle_final_response(context, state, turn) -> PostTurnOutcome:
    request = context.request
    setup = context.setup
    outcome = final_response_flow.handle_final_response(
        final_response_flow.FinalResponseContext(
            session=context.session,
            conversation=state.conversation,
            saved_message=turn.saved_message,
            user_id=request.user_id,
            ai_config_id=request.ai_config_id,
            ai_kind=request.ai_kind,
            session_id=request.session_id,
            session_name=state.session_name,
            model=setup.model,
            config=setup.config,
            warning_template=setup.warning_template,
            assistant_text=turn.assistant_text,
            native_tool_calls=turn.native_tool_calls,
            phase_started_at=state.phase_started_at,
            set_live_phase=context.set_live_phase,
            auto_finalize_plan=context.auto_finalize_plan,
            notify_task_completion=send_task_completion_notification,
        ),
        final_response_flow.FinalResponseState(
            markup_fallback_available=state.markup_fallback_available,
            pending_ai_reply_message_id=state.pending_reply_message_id,
            plan_state=state.plan_state,
            awaiting_finish=state.awaiting_finish,
            task_job=state.task_job,
        ),
    )
    next_state = replace(
        state,
        markup_fallback_available=outcome.state.markup_fallback_available,
        pending_reply_message_id=outcome.state.pending_ai_reply_message_id,
        plan_state=outcome.state.plan_state,
        awaiting_finish=outcome.state.awaiting_finish,
    )
    action = (
        PostTurnAction.NEXT_TURN
        if outcome.action is final_response_flow.FinalResponseAction.NEXT_TURN
        else PostTurnAction.COMPLETE_RUN
    )
    return PostTurnOutcome(action, next_state)
