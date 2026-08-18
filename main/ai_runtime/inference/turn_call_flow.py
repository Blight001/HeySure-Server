"""Explicit state machine for one model-produced tool call."""

from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional

from sqlmodel import Session

from api.chat_runtime.chat_prompt_utils import _append_mcp_state_to_tags
from api.models import ChatMessage
from ai_runtime.inference import (
    phase_context,
    plan_transitions,
    tool_media,
    tool_metadata,
    tool_persistence,
    tool_rejections,
)
from ai_runtime.inference.tool_execution import JoinedToolRequest, execute_tool_call
from ai_runtime.inference.transaction_boundary import (
    release_clean_session_before_external_io,
)
from ai_runtime.inference.tool_resolution import (
    ToolResponseContext,
    TurnCallAction,
    append_joined_tool_response,
    append_ordinary_tool_response,
    known_mcp_tool_names,
    resolve_mcp_tool_name,
    split_concatenated_native_tool_name,
)
from api.services.mcp.mcp_tool_aliases import apply_legacy_desktop_call


@dataclass(frozen=True)
class TurnCallContext:
    session: Session
    conversation: List[Dict]
    screenshot_messages: List[Dict]
    saved_message: ChatMessage
    user_id: int
    ai_config_id: Optional[int]
    ai_kind: str
    session_id: str
    model: str
    run_id: str
    config: Any
    effective_tools: frozenset[str]
    native_tool_name_map: Dict[str, str]
    native_tool_calls: bool
    system_prompt: str
    current_user_message_id: Optional[int]
    model_user_content: Optional[str]
    turn_conversation_start: int
    image_input_disabled: bool
    should_stop: Callable[[], bool]
    stop_run: Callable[[], None]
    complete_run: Callable[[], None]
    set_live_phase: Callable[..., None]
    set_run_error: Callable[[str], None]
    auto_finalize_plan: Callable[[float], None]


@dataclass(frozen=True)
class TurnCallState:
    session_name: str
    exposed_tools: frozenset[str]
    rejected_tool_signature: str
    rejected_repeat: int
    plan: plan_transitions.PlanFlowSnapshot


@dataclass
class TurnCallMachine:
    context: TurnCallContext
    state: TurnCallState

    def execute(
        self,
        call: Dict[str, Any],
        pending: List[Dict[str, Any]],
    ) -> TurnCallAction:
        raw_tool = str(call.get("tool") or "")
        tool = resolve_mcp_tool_name(
            raw_tool,
            self.context.native_tool_name_map,
            set(self.context.effective_tools),
        )
        arguments = apply_legacy_desktop_call(raw_tool, tool, call.get("arguments") or {})
        call_id = str(call.get("id") or "call_0")
        if self.context.should_stop():
            self.context.stop_run()
            return TurnCallAction.STOP_RUN
        rejection_context = self._rejection_context(pending)
        if self.context.config and not self.context.config.mcp_enabled:
            return self._reject_disabled(
                rejection_context, tool, arguments, call_id
            )
        joined_tools = split_concatenated_native_tool_name(
            tool,
            self.context.native_tool_name_map,
        )
        if joined_tools:
            return self._execute_joined(
                tool, joined_tools, arguments, call_id
            )
        if tool not in self.context.effective_tools:
            return self._reject_disallowed(
                rejection_context, tool, arguments, call_id,
                raw_tool=raw_tool,
            )
        return self._execute_regular(tool, arguments, call_id, pending)

    def _rejection_context(self, pending) -> tool_rejections.RejectionContext:
        context = self.context
        return tool_rejections.RejectionContext(
            session=context.session,
            conversation=context.conversation,
            pending=pending,
            saved_message=context.saved_message,
            user_id=context.user_id,
            ai_config_id=context.ai_config_id,
            ai_kind=context.ai_kind,
            session_id=context.session_id,
            session_name=self.state.session_name,
            model=context.model,
            run_id=context.run_id,
            native_tool_calls=context.native_tool_calls,
            set_live_phase=context.set_live_phase,
            set_run_error=context.set_run_error,
        )

    def _reject_disabled(self, rejection_context, tool, arguments, call_id):
        outcome = tool_rejections.handle_mcp_disabled(
            rejection_context,
            tool,
            arguments,
            call_id,
            self.state.rejected_tool_signature,
            self.state.rejected_repeat,
        )
        self._set_rejection_state(outcome)
        return outcome.action

    def _reject_disallowed(self, rejection_context, tool, arguments, call_id, *, raw_tool=""):
        outcome = tool_rejections.handle_disallowed_tool(
            rejection_context,
            tool,
            arguments,
            call_id,
            self.context.effective_tools,
            self.state.rejected_tool_signature,
            self.state.rejected_repeat,
            tool_rejections.ToolResolutionInfo(
                raw_tool=raw_tool,
                known_tools=frozenset(known_mcp_tool_names()),
            ),
        )
        self._set_rejection_state(outcome)
        return outcome.action

    def _set_rejection_state(self, outcome) -> None:
        self.state = replace(
            self.state,
            rejected_tool_signature=outcome.signature,
            rejected_repeat=outcome.count,
        )

    def _execute_joined(self, tool, joined_tools, arguments, call_id):
        context = self.context
        mapped_tools = [
            context.native_tool_name_map.get(item, item)
            for item in joined_tools
        ]
        outcome = tool_persistence.execute_and_persist_joined_batch(
            JoinedToolRequest(
                tools=tuple(mapped_tools),
                arguments=arguments,
                allowed_tools=context.effective_tools,
                user_id=context.user_id,
                ai_config_id=context.ai_config_id,
            ),
            tool_persistence.JoinedPersistenceContext(
                session=context.session,
                saved_message=context.saved_message,
                user_id=context.user_id,
                ai_config_id=context.ai_config_id,
                ai_kind=context.ai_kind,
                session_id=context.session_id,
                session_name=self.state.session_name,
                model=context.model,
                run_id=context.run_id,
                plan_active=self.state.plan.plan_state is not None,
                phase_mcp_statuses=self.state.plan.phase_mcp_statuses,
                should_stop=context.should_stop,
                mark_waiting=self._mark_joined_waiting,
            ),
        )
        if outcome.stopped:
            context.stop_run()
            return TurnCallAction.STOP_RUN
        append_joined_tool_response(
            context.conversation,
            tool,
            outcome.items,
            outcome.failed,
            call_id,
            native=context.native_tool_calls,
        )
        return TurnCallAction.NEXT_CALL

    def _execute_regular(self, tool, arguments, call_id, pending):
        context = self.context
        context.set_live_phase("waiting_mcp", tool, arguments)
        release_clean_session_before_external_io(
            context.session,
            boundary=f"MCP tool {tool}",
        )
        execution = execute_tool_call(
            tool,
            context.user_id,
            arguments,
            context.ai_config_id,
        )
        self._record_execution(tool, execution)
        self._apply_metadata(tool, execution.result, execution.failed)
        self._persist_execution(tool, arguments, execution)
        transition = self._plan_transition(
            tool, arguments, call_id, pending, execution
        )
        if transition is not None:
            self.state = replace(self.state, plan=transition.snapshot)
            return transition.action
        append_ordinary_tool_response(
            ToolResponseContext(
                conversation=context.conversation,
                screenshot_messages=context.screenshot_messages,
                turn_convo_start=context.turn_conversation_start,
                image_input_disabled=context.image_input_disabled,
                native_tool_calls=context.native_tool_calls,
            ),
            tool,
            arguments,
            execution.result,
            execution.failed,
            call_id,
        )
        return TurnCallAction.NEXT_CALL

    def _mark_joined_waiting(self, tool: str, arguments) -> None:
        release_clean_session_before_external_io(
            self.context.session,
            boundary=f"MCP tool {tool}",
        )
        self.context.set_live_phase("waiting_mcp", tool, arguments)

    def _record_execution(self, tool, execution) -> None:
        context = self.context
        tool_persistence.record_tool_call(tool_persistence.ToolCallRecord(
            tool=tool,
            user_id=context.user_id,
            ai_config_id=context.ai_config_id,
            session_id=context.session_id,
            run_id=context.run_id,
            message_id=getattr(context.saved_message, "id", None),
            failed=execution.failed,
            error=execution.error,
        ))
        if self.state.plan.plan_state is not None:
            phase_context.record_status(
                self.state.plan.phase_mcp_statuses,
                tool,
                execution.failed,
            )

    def _apply_metadata(self, tool, tool_result, failed) -> None:
        context = self.context
        exposed_tools = set(self.state.exposed_tools)
        renamed_session = tool_metadata.apply_tool_metadata(
            tool_metadata.ToolMetadataContext(
                session=context.session,
                user_id=context.user_id,
                ai_config_id=context.ai_config_id,
                ai_kind=context.ai_kind,
                session_id=context.session_id,
                session_name=self.state.session_name,
                allowed_tools=context.effective_tools,
                exposed_tools=exposed_tools,
            ),
            tool,
            tool_result,
            failed,
        )
        session_name = tool_metadata.apply_session_rename(
            context.saved_message,
            self.state.session_name,
            renamed_session,
        )
        self.state = replace(
            self.state,
            session_name=session_name,
            exposed_tools=frozenset(exposed_tools),
        )

    def _persist_execution(self, tool, arguments, execution) -> None:
        context = self.context
        saved = context.saved_message
        saved.tags = _append_mcp_state_to_tags(
            saved.tags,
            tool,
            arguments,
            execution.display_text,
        )
        context.session.add(saved)
        context.session.commit()
        screenshot = (
            {}
            if execution.failed
            else tool_media.screenshot_display_ref(tool, execution.result)
        )
        tool_persistence.save_tool_bubble(tool_persistence.ToolBubbleRequest(
            session=context.session,
            user_id=context.user_id,
            ai_config_id=context.ai_config_id,
            ai_kind=context.ai_kind,
            session_id=context.session_id,
            session_name=self.state.session_name,
            model=context.model,
            tool=tool,
            arguments=arguments,
            result_text=execution.display_text,
            failed=execution.failed,
            image_url=screenshot.get("url", ""),
            image_data_url=screenshot.get("data_url", ""),
            tool_result=(
                execution.result
                if isinstance(execution.result, dict)
                else None
            ),
            latency=execution.latency,
        ))

    def _plan_transition(self, tool, arguments, call_id, pending, execution):
        context = self.context
        return plan_transitions.handle_plan_transition(
            plan_transitions.PlanTransitionContext(
                session=context.session,
                conversation=context.conversation,
                pending=pending,
                screenshot_messages=context.screenshot_messages,
                user_id=context.user_id,
                ai_config_id=context.ai_config_id,
                ai_kind=context.ai_kind,
                session_id=context.session_id,
                session_name=self.state.session_name,
                model=context.model,
                native_tool_calls=context.native_tool_calls,
                system_prompt=context.system_prompt,
                current_user_message_id=context.current_user_message_id,
                model_user_content=context.model_user_content,
                set_live_phase=context.set_live_phase,
                complete_run=context.complete_run,
                auto_finalize_plan=context.auto_finalize_plan,
            ),
            self.state.plan,
            plan_transitions.ControlToolCall(
                tool=tool,
                arguments=arguments,
                tool_result=execution.result,
                failed=execution.failed,
                call_id=call_id,
            ),
        )
