"""Run, recover and persist one model turn for the inference worker loop."""

from dataclasses import dataclass
from enum import Enum
import time
from typing import Callable, Dict, List, Optional

from sqlmodel import Session

from api.chat_runtime.chat_prompt_utils import _extract_mcp_error
from ai_runtime.inference import (
    model_error_flow,
    model_gateway,
    step_preparation,
    tool_media,
    turn_result,
)
from ai_runtime.inference.debug_support import (
    ai_debug_stage,
    ai_short,
    ai_short_run_id,
)
from ai_runtime.inference.transaction_boundary import (
    release_clean_session_before_external_io,
)


class WorkerTurnAction(Enum):
    PROCEED = "proceed"
    RETRY = "retry"
    STOP_RUN = "stop_run"


@dataclass(frozen=True)
class WorkerTurnContext:
    session: Session
    conversation: List[Dict]
    user_id: int
    ai_config_id: Optional[int]
    ai_kind: str
    session_id: str
    model: str
    system_prompt: str
    run_id: str
    provider: str
    base_url: str
    api_key: str
    headers: Dict[str, str]
    should_stop: Callable[[], bool]
    stop_run: Callable[[], None]
    set_live_phase: Callable[[str], None]
    set_run_error: Callable[[str], None]
    clear_live_text: Callable[[], None]
    reset_live_usage: Callable[[], None]
    reasoning_effort: str = ""


@dataclass(frozen=True)
class WorkerTurnState:
    pending_reply_message_id: str
    consecutive_errors: int
    image_input_disabled: bool


@dataclass(frozen=True)
class WorkerTurnPolicy:
    mcp_active: bool
    exposed_tools: frozenset[str]
    allowed_tools: frozenset[str]
    task_runtime: bool
    plan_active: bool
    awaiting_finish: bool
    tool_protocol: str


@dataclass(frozen=True)
class WorkerTurnRequest:
    step_label: str
    session_name: str
    state: WorkerTurnState
    policy: WorkerTurnPolicy


@dataclass(frozen=True)
class WorkerTurnOutcome:
    action: WorkerTurnAction
    state: WorkerTurnState
    assistant_text: str = ""
    native_tool_calls: bool = False
    native_tool_name_map: Optional[Dict[str, str]] = None
    persisted_turn: Optional[turn_result.PersistedAssistantTurn] = None


def run_worker_turn(
    context: WorkerTurnContext,
    request: WorkerTurnRequest,
) -> WorkerTurnOutcome:
    state = _prepare_messages(context, request)
    exposure = step_preparation.select_tool_exposure(
        step_preparation.ToolExposureRequest(
            mcp_active=request.policy.mcp_active,
            exposed_tools=request.policy.exposed_tools,
            allowed_tools=request.policy.allowed_tools,
            task_runtime=request.policy.task_runtime,
            plan_active=request.policy.plan_active,
            awaiting_finish=request.policy.awaiting_finish,
            tool_protocol=request.policy.tool_protocol,
        )
    )
    _debug_turn_start(context, request.step_label, state, exposure)
    started_at = time.time()
    try:
        release_clean_session_before_external_io(
            context.session,
            boundary="model request",
        )
        stream_result = model_gateway.run_model_turn(model_gateway.ModelTurnRequest(
            run_id=context.run_id,
            provider=context.provider,
            base_url=context.base_url,
            api_key=context.api_key,
            model=context.model,
            conversation=context.conversation,
            provider_tools=exposure.provider_tools,
            native_name_map=exposure.native_name_map,
            headers=context.headers,
            reasoning_effort=context.reasoning_effort,
        ))
    except Exception as exc:
        return _handle_error(context, request, state, exc)
    state = WorkerTurnState(
        pending_reply_message_id=state.pending_reply_message_id,
        consecutive_errors=0,
        image_input_disabled=state.image_input_disabled,
    )
    if stream_result.stopped or context.should_stop():
        context.stop_run()
        return WorkerTurnOutcome(WorkerTurnAction.STOP_RUN, state)
    latency = time.time() - started_at
    persisted = turn_result.persist_assistant_turn(
        turn_result.AssistantTurnContext(
            session=context.session,
            conversation=context.conversation,
            user_id=context.user_id,
            ai_config_id=context.ai_config_id,
            ai_kind=context.ai_kind,
            session_id=context.session_id,
            session_name=request.session_name,
            model=context.model,
            system_prompt=context.system_prompt,
            native_tool_name_map=exposure.native_name_map,
            allowed_tools=request.policy.allowed_tools,
        ),
        stream_result,
        latency,
    )
    _debug_turn_done(
        context,
        request.step_label,
        stream_result,
        persisted,
        latency,
    )
    context.clear_live_text()
    context.reset_live_usage()
    return WorkerTurnOutcome(
        action=WorkerTurnAction.PROCEED,
        state=state,
        assistant_text=stream_result.assistant_text,
        native_tool_calls=stream_result.has_native_tc,
        native_tool_name_map=exposure.native_name_map,
        persisted_turn=persisted,
    )


def _prepare_messages(context, request) -> WorkerTurnState:
    pending_reply = step_preparation.ingest_step_messages(
        step_preparation.StepMessageContext(
            session=context.session,
            conversation=context.conversation,
            user_id=context.user_id,
            ai_config_id=context.ai_config_id,
            ai_kind=context.ai_kind,
            session_id=context.session_id,
            session_name=request.session_name,
            model=context.model,
        ),
        request.state.pending_reply_message_id,
    )
    model_error_flow.repair_missing_tool_responses(
        context.conversation,
        "Synthetic tool result inserted before request because the previous tool call did not receive a tool response.",
    )
    if request.state.image_input_disabled:
        removed = tool_media.degrade_image_messages_to_text(context.conversation)
        if removed:
            context.conversation.append({
                "role": "user",
                "content": tool_media.image_input_degraded_feedback(
                    "The current model previously rejected image input in this run.",
                    removed,
                ),
            })
    return WorkerTurnState(
        pending_reply_message_id=pending_reply,
        consecutive_errors=request.state.consecutive_errors,
        image_input_disabled=request.state.image_input_disabled,
    )


def _handle_error(context, request, state, exc) -> WorkerTurnOutcome:
    error_text = _extract_mcp_error(exc)
    ai_debug_stage(
        "ERR",
        f"{ai_short_run_id(context.run_id)} #{request.step_label} "
        f"x{state.consecutive_errors + 1} {ai_short(error_text, 140)}",
        "31",
    )
    decision = model_error_flow.handle_model_error(
        model_error_flow.ModelErrorContext(
            session=context.session,
            conversation=context.conversation,
            user_id=context.user_id,
            ai_config_id=context.ai_config_id,
            ai_kind=context.ai_kind,
            session_id=context.session_id,
            session_name=request.session_name,
            model=context.model,
            set_generating=lambda: context.set_live_phase("generating"),
            set_run_error=context.set_run_error,
        ),
        error_text,
        state.consecutive_errors,
        state.image_input_disabled,
    )
    next_state = WorkerTurnState(
        pending_reply_message_id=state.pending_reply_message_id,
        consecutive_errors=decision.consecutive_errors,
        image_input_disabled=decision.image_input_disabled,
    )
    action = (
        WorkerTurnAction.STOP_RUN
        if decision.stop_run
        else WorkerTurnAction.RETRY
    )
    return WorkerTurnOutcome(action, next_state)


def _debug_turn_start(context, step_label, state, exposure) -> None:
    ai_debug_stage(
        "TURN",
        f"{ai_short_run_id(context.run_id)} #{step_label} "
        f"start msgs={len(context.conversation)} tools={len(exposure.provider_tools)} "
        f"reply={'y' if state.pending_reply_message_id else 'n'}",
        "33",
    )


def _debug_turn_done(context, step_label, stream_result, persisted, latency) -> None:
    ai_debug_stage(
        "DONE",
        f"{ai_short_run_id(context.run_id)} #{step_label} "
        f"{stream_result.finish_reason or 'stop'} {int(latency * 1000)}ms "
        f"tok={persisted.token_triplet} "
        f"tc={'native:' if stream_result.has_native_tc else ''}"
        f"{ai_short(', '.join(c['tool'] for c in persisted.tool_calls) or '-', 48)}",
        "32",
    )
