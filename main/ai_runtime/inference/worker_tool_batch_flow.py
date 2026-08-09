"""Tool-batch orchestration after a model turn has passed flow gates."""

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Callable, Dict, List

from sqlmodel import Session

from ai_runtime.inference import plan_transitions, tool_batch_flow, turn_call_flow
from ai_runtime.inference.debug_support import (
    ai_debug_stage,
    ai_short,
    ai_short_run_id,
)
from ai_runtime.inference.run_request import WorkerRequest
from ai_runtime.inference.tool_resolution import (
    TurnCallAction,
    flush_screenshot_messages,
)


duplicate_call_flags = tool_batch_flow.duplicate_call_flags
PlanFlowSnapshot = plan_transitions.PlanFlowSnapshot


class WorkerToolBatchAction(Enum):
    NEXT_TURN = "next_turn"
    STOP_RUN = "stop_run"


@dataclass(frozen=True)
class WorkerToolBatchContext:
    session: Session
    request: WorkerRequest
    config: Any
    model: str
    system_prompt: str
    conversation: List[Dict]
    saved_message: Any
    effective_tools: frozenset[str]
    native_tool_name_map: Dict[str, str]
    native_tool_calls: bool
    turn_conversation_start: int
    image_input_disabled: bool
    screenshot_messages: List[Dict]
    should_stop: Callable[[], bool]
    stop_run: Callable[[], None]
    complete_run: Callable[[], None]
    set_live_phase: Callable[..., None]
    set_run_error: Callable[[str], None]
    auto_finalize_plan: Callable[[float], None]


@dataclass(frozen=True)
class WorkerToolBatchState:
    session_name: str
    exposed_tools: frozenset[str]
    rejected_tool_signature: str
    rejected_repeat: int
    plan: PlanFlowSnapshot
    last_batch_signature: str
    consecutive_same_batch: int


@dataclass(frozen=True)
class WorkerToolBatchData:
    step_label: str
    turn_calls: List[Dict]


@dataclass(frozen=True)
class WorkerToolBatchOutcome:
    action: WorkerToolBatchAction
    state: WorkerToolBatchState


def handle_tool_batch(
    context: WorkerToolBatchContext,
    state: WorkerToolBatchState,
    data: WorkerToolBatchData,
) -> WorkerToolBatchOutcome:
    progress = tool_batch_flow.evaluate_progress(
        _progress_context(context, state),
        tool_batch_flow.ProgressState(
            last_batch_signature=state.last_batch_signature,
            consecutive_same_batch=state.consecutive_same_batch,
        ),
        data.turn_calls,
    )
    state = replace(
        state,
        last_batch_signature=progress.state.last_batch_signature,
        consecutive_same_batch=progress.state.consecutive_same_batch,
    )
    if progress.action is not tool_batch_flow.ProgressAction.EXECUTE_BATCH:
        _debug_no_progress(context, state, data)
        if progress.action is tool_batch_flow.ProgressAction.STOP_RUN:
            context.complete_run()
            return WorkerToolBatchOutcome(WorkerToolBatchAction.STOP_RUN, state)
        return WorkerToolBatchOutcome(WorkerToolBatchAction.NEXT_TURN, state)

    machine = turn_call_flow.TurnCallMachine(
        _turn_call_context(context),
        turn_call_flow.TurnCallState(
            session_name=state.session_name,
            exposed_tools=state.exposed_tools,
            rejected_tool_signature=state.rejected_tool_signature,
            rejected_repeat=state.rejected_repeat,
            plan=state.plan,
        ),
    )
    batch_action = tool_batch_flow.execute_turn_batch(
        context.conversation,
        data.turn_calls,
        context.native_tool_calls,
        machine.execute,
        lambda call: _debug_duplicate(context, data.step_label, call),
    )
    state = replace(
        state,
        session_name=machine.state.session_name,
        exposed_tools=machine.state.exposed_tools,
        rejected_tool_signature=machine.state.rejected_tool_signature,
        rejected_repeat=machine.state.rejected_repeat,
        plan=machine.state.plan,
    )
    if batch_action is TurnCallAction.STOP_RUN:
        return WorkerToolBatchOutcome(WorkerToolBatchAction.STOP_RUN, state)
    if batch_action is TurnCallAction.NEXT_CALL:
        flush_screenshot_messages(
            context.conversation,
            context.screenshot_messages,
        )
        context.set_live_phase("generating")
    return WorkerToolBatchOutcome(WorkerToolBatchAction.NEXT_TURN, state)


def _progress_context(context, state) -> tool_batch_flow.ProgressContext:
    request = context.request
    return tool_batch_flow.ProgressContext(
        session=context.session,
        conversation=context.conversation,
        user_id=request.user_id,
        ai_config_id=request.ai_config_id,
        ai_kind=request.ai_kind,
        session_id=request.session_id,
        session_name=state.session_name,
        model=context.model,
        native_tool_calls=context.native_tool_calls,
        set_live_phase=lambda phase: context.set_live_phase(phase),
    )


def _turn_call_context(context) -> turn_call_flow.TurnCallContext:
    request = context.request
    return turn_call_flow.TurnCallContext(
        session=context.session,
        conversation=context.conversation,
        screenshot_messages=context.screenshot_messages,
        saved_message=context.saved_message,
        user_id=request.user_id,
        ai_config_id=request.ai_config_id,
        ai_kind=request.ai_kind,
        session_id=request.session_id,
        model=context.model,
        run_id=request.run_id,
        config=context.config,
        effective_tools=context.effective_tools,
        native_tool_name_map=context.native_tool_name_map,
        native_tool_calls=context.native_tool_calls,
        system_prompt=context.system_prompt,
        current_user_message_id=request.current_user_message_id,
        model_user_content=request.model_user_content,
        turn_conversation_start=context.turn_conversation_start,
        image_input_disabled=context.image_input_disabled,
        should_stop=context.should_stop,
        stop_run=context.stop_run,
        complete_run=context.complete_run,
        set_live_phase=context.set_live_phase,
        set_run_error=context.set_run_error,
        auto_finalize_plan=context.auto_finalize_plan,
    )


def _debug_no_progress(context, state, data) -> None:
    ai_debug_stage(
        "LOOP",
        f"{ai_short_run_id(context.request.run_id)} #{data.step_label} "
        f"x{state.consecutive_same_batch} "
        f"{ai_short(', '.join(call['tool'] for call in data.turn_calls), 48)}",
        "31",
    )


def _debug_duplicate(context, step_label, turn_call) -> None:
    ai_debug_stage(
        "DEDUP",
        f"{ai_short_run_id(context.request.run_id)} #{step_label} "
        f"{ai_short(str(turn_call.get('tool') or '?'), 40)}",
        "33",
    )
