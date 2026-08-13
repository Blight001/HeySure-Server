"""Top-level inference worker loop with explicit mutable run state."""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum

from api.chat_runtime.chat_prompt_utils import (
    _set_run_live_phase,
    _set_run_live_text,
    _set_run_live_usage,
)
from api.chat_runtime.chat_runtime_helpers import _run_set_status, _run_should_stop
from api.models import ChatMessageCreate
from api.runtime import run_context
from api.services.chat.chat_persistence import _save_message
from api.services.tasks import task_plan as plan_service
from ai_runtime.inference import (
    phase_context,
    worker_post_turn_flow,
    worker_setup,
    worker_tool_batch_flow,
    worker_turn_flow,
)
from ai_runtime.inference.debug_support import (
    ai_debug_stage,
    ai_short_base_url,
    ai_short_run_id,
)
from ai_runtime.inference.plan_flow import (
    PlanFinalizeContext,
    append_plan_directive,
    finalize_plan,
)
from ai_runtime.inference.policies import (
    can_start_inference_step,
    has_active_todo_plan,
)
from ai_runtime.inference.run_request import WorkerRequest


logger = logging.getLogger(__name__)


class WorkerRunStepAction(Enum):
    NEXT_TURN = "next_turn"
    STOP_RUN = "stop_run"


@dataclass
class WorkerRunState:
    conversation: list[dict]
    session_name: str
    task_job: object
    exposed_tools: set[str]
    pending_reply_message_id: str = ""
    consecutive_ai_errors: int = 0
    image_input_disabled: bool = False
    rejected_tool_signature: str = ""
    rejected_repeat: int = 0
    last_batch_signature: str = ""
    consecutive_same_batch: int = 0
    plan_state: object = None
    awaiting_finish: bool = False
    phase_start_conversation_index: int = 0
    phase_started_at: float = 0.0
    phase_mcp_statuses: list[tuple] = field(default_factory=list)
    markup_fallback_available: bool = True
    compression_failed: bool = False
    completed_steps: int = 0

@dataclass
class WorkerRunMachine:
    session: object
    request: WorkerRequest
    setup: worker_setup.WorkerSetup
    capabilities: worker_setup.WorkerCapabilities
    state: WorkerRunState

    @classmethod
    def create(cls, session: object, request: WorkerRequest) -> "WorkerRunMachine":
        setup = worker_setup.prepare_worker(session, request)
        capabilities = worker_setup.prepare_capabilities(session, request, setup)
        state = WorkerRunState(
            conversation=setup.conversation,
            session_name=request.session_name,
            task_job=setup.task_job,
            exposed_tools=set(capabilities.exposed_tool_allowlist),
            phase_start_conversation_index=len(setup.conversation),
            phase_started_at=time.time(),
        )
        machine = cls(session, request, setup, capabilities, state)
        machine._initialize()
        return machine

    def run(self) -> None:
        while can_start_inference_step(
            self.state.completed_steps,
            self.setup.max_steps,
            self.state.plan_state,
        ):
            self.state.completed_steps += 1
            if self.should_stop():
                self.stop_run()
                return
            if self._run_step() is WorkerRunStepAction.STOP_RUN:
                return
        self._save_step_limit_notice()
        self.complete_run()

    def _initialize(self) -> None:
        self._debug_initial_state()
        self._set_runtime_context()
        self._load_plan()
        self._anchor_plan()

    def _debug_initial_state(self) -> None:
        ai_debug_stage(
            "INIT",
            f"{ai_short_run_id(self.request.run_id)} "
            f"{self.capabilities.provider} {self.setup.model} "
            f"tc_proto={self.capabilities.tool_protocol} "
            f"host={ai_short_base_url(self.setup.base_url)} "
            f"hist={len(self.setup.history)} "
            f"tools={len(self.setup.effective_tool_allowlist)}/"
            f"{len(self.state.exposed_tools)} "
            f"mcp={'on' if self.capabilities.mcp_active else 'off'}",
            "34",
        )

    def _set_runtime_context(self) -> None:
        request = self.request
        context = {
            "run_id": request.run_id,
            "user_id": request.user_id,
            "ai_config_id": request.ai_config_id,
            "ai_kind": request.ai_kind,
            "session_id": request.session_id,
            "session_name": self.state.session_name,
            "model": self.setup.model,
            "current_user_message_id": request.current_user_message_id,
        }
        run_context.set_run_session_context(run_context.enrich_bot_scope(self.session, context))

    def _load_plan(self) -> None:
        request = self.request
        if request.ai_config_id is None:
            return
        try:
            self.state.plan_state = plan_service.get_active_plan(
                self.session,
                request.user_id,
                int(request.ai_config_id),
                request.session_id,
            )
            self.state.awaiting_finish = plan_service.awaiting_finish(
                self.session,
                self.state.plan_state,
            )
        except Exception:
            logger.exception("plan state load failed")
            self.state.plan_state = None

    def _anchor_plan(self) -> None:
        was_awaiting_finish = self.state.awaiting_finish
        if self.state.awaiting_finish:
            self.auto_finalize_plan(self.state.phase_started_at)
        if self.state.plan_state is not None:
            self.inject_flow_directive(self.state.conversation)
        elif (
            self.setup.is_task_runtime
            and self.request.ai_config_id is not None
            and not was_awaiting_finish
        ):
            self.state.conversation.append({
                "role": "user",
                "content": phase_context.render_plan_required_notice(),
            })

    def _run_step(self) -> WorkerRunStepAction:
        turn = self._run_model_turn()
        if turn.action is worker_turn_flow.WorkerTurnAction.STOP_RUN:
            return WorkerRunStepAction.STOP_RUN
        if turn.action is worker_turn_flow.WorkerTurnAction.RETRY:
            return WorkerRunStepAction.NEXT_TURN
        if turn.persisted_turn is None:
            raise RuntimeError("model turn proceeded without persistence")
        post_turn = self._run_post_turn(turn)
        if post_turn.action is worker_post_turn_flow.PostTurnAction.NEXT_TURN:
            return WorkerRunStepAction.NEXT_TURN
        if post_turn.action is worker_post_turn_flow.PostTurnAction.COMPLETE_RUN:
            self.complete_run()
            return WorkerRunStepAction.STOP_RUN
        return self._run_tool_batch(turn)

    def _run_model_turn(self) -> worker_turn_flow.WorkerTurnOutcome:
        request = self.request
        state = self.state
        outcome = worker_turn_flow.run_worker_turn(
            worker_turn_flow.WorkerTurnContext(
                session=self.session,
                conversation=state.conversation,
                user_id=request.user_id,
                ai_config_id=request.ai_config_id,
                ai_kind=request.ai_kind,
                session_id=request.session_id,
                model=self.setup.model,
                system_prompt=self.setup.system_prompt,
                run_id=request.run_id,
                provider=self.capabilities.provider,
                base_url=self.setup.base_url,
                api_key=self.setup.api_key,
                headers=self.capabilities.headers,
                should_stop=self.should_stop,
                stop_run=self.stop_run,
                set_live_phase=self.set_live_phase,
                set_run_error=self.set_run_error,
                clear_live_text=self.clear_live_text,
                reset_live_usage=self.reset_live_usage,
            ),
            worker_turn_flow.WorkerTurnRequest(
                step_label=self.step_label(),
                session_name=state.session_name,
                state=worker_turn_flow.WorkerTurnState(
                    pending_reply_message_id=state.pending_reply_message_id,
                    consecutive_errors=state.consecutive_ai_errors,
                    image_input_disabled=state.image_input_disabled,
                ),
                policy=worker_turn_flow.WorkerTurnPolicy(
                    mcp_active=self.capabilities.mcp_active,
                    exposed_tools=frozenset(state.exposed_tools),
                    allowed_tools=frozenset(self.setup.effective_tool_allowlist),
                    task_runtime=self.setup.is_task_runtime,
                    plan_active=state.plan_state is not None,
                    awaiting_finish=state.awaiting_finish,
                    tool_protocol=self.capabilities.tool_protocol,
                ),
            ),
        )
        state.pending_reply_message_id = outcome.state.pending_reply_message_id
        state.consecutive_ai_errors = outcome.state.consecutive_errors
        state.image_input_disabled = outcome.state.image_input_disabled
        return outcome

    def _run_post_turn(self, turn) -> worker_post_turn_flow.PostTurnOutcome:
        persisted = turn.persisted_turn
        outcome = worker_post_turn_flow.handle_post_turn(
            worker_post_turn_flow.PostTurnContext(
                session=self.session,
                request=self.request,
                setup=self.setup,
                reset_live_usage=self.reset_live_usage,
                set_live_phase=self.set_live_phase,
                inject_flow_directive=self.inject_flow_directive,
                auto_finalize_plan=self.auto_finalize_plan,
            ),
            self._post_turn_state(),
            worker_post_turn_flow.PostTurnData(
                saved_message=persisted.saved_message,
                assistant_text=turn.assistant_text,
                native_tool_calls=turn.native_tool_calls,
                turn_calls=persisted.tool_calls,
            ),
        )
        self._apply_post_turn_state(outcome.state)
        return outcome

    def _post_turn_state(self) -> worker_post_turn_flow.PostTurnState:
        state = self.state
        return worker_post_turn_flow.PostTurnState(
            conversation=state.conversation,
            session_name=state.session_name,
            plan_state=state.plan_state,
            awaiting_finish=state.awaiting_finish,
            phase_start_convo_index=state.phase_start_conversation_index,
            phase_started_at=state.phase_started_at,
            phase_mcp_statuses=state.phase_mcp_statuses,
            compression_failed=state.compression_failed,
            task_job=state.task_job,
            markup_fallback_available=state.markup_fallback_available,
            pending_reply_message_id=state.pending_reply_message_id,
        )

    def _apply_post_turn_state(self, post_state) -> None:
        state = self.state
        state.conversation = post_state.conversation
        state.session_name = post_state.session_name
        state.plan_state = post_state.plan_state
        state.awaiting_finish = post_state.awaiting_finish
        state.phase_start_conversation_index = post_state.phase_start_convo_index
        state.phase_started_at = post_state.phase_started_at
        state.phase_mcp_statuses = post_state.phase_mcp_statuses
        state.compression_failed = post_state.compression_failed
        state.task_job = post_state.task_job
        state.markup_fallback_available = post_state.markup_fallback_available
        state.pending_reply_message_id = post_state.pending_reply_message_id

    def _run_tool_batch(self, turn) -> WorkerRunStepAction:
        persisted = turn.persisted_turn
        outcome = worker_tool_batch_flow.handle_tool_batch(
            self._tool_batch_context(turn),
            self._tool_batch_state(),
            worker_tool_batch_flow.WorkerToolBatchData(
                step_label=self.step_label(),
                turn_calls=persisted.tool_calls,
            ),
        )
        self._apply_tool_batch_state(outcome.state)
        if outcome.action is worker_tool_batch_flow.WorkerToolBatchAction.STOP_RUN:
            return WorkerRunStepAction.STOP_RUN
        return WorkerRunStepAction.NEXT_TURN

    def _tool_batch_context(self, turn):
        persisted = turn.persisted_turn
        return worker_tool_batch_flow.WorkerToolBatchContext(
            session=self.session,
            request=self.request,
            config=self.setup.config,
            model=self.setup.model,
            system_prompt=self.setup.system_prompt,
            conversation=self.state.conversation,
            saved_message=persisted.saved_message,
            effective_tools=frozenset(self.setup.effective_tool_allowlist),
            native_tool_name_map=turn.native_tool_name_map or {},
            native_tool_calls=turn.native_tool_calls,
            turn_conversation_start=persisted.conversation_start,
            image_input_disabled=self.state.image_input_disabled,
            screenshot_messages=[],
            should_stop=self.should_stop,
            stop_run=self.stop_run,
            complete_run=self.complete_run,
            set_live_phase=self.set_live_phase,
            set_run_error=self.set_run_error,
            auto_finalize_plan=self.auto_finalize_plan,
        )

    def _tool_batch_state(self) -> worker_tool_batch_flow.WorkerToolBatchState:
        state = self.state
        return worker_tool_batch_flow.WorkerToolBatchState(
            session_name=state.session_name,
            exposed_tools=frozenset(state.exposed_tools),
            rejected_tool_signature=state.rejected_tool_signature,
            rejected_repeat=state.rejected_repeat,
            plan=worker_tool_batch_flow.PlanFlowSnapshot(
                plan_state=state.plan_state,
                awaiting_finish=state.awaiting_finish,
                phase_start_convo_index=state.phase_start_conversation_index,
                phase_started_at=state.phase_started_at,
                phase_mcp_statuses=state.phase_mcp_statuses,
            ),
            last_batch_signature=state.last_batch_signature,
            consecutive_same_batch=state.consecutive_same_batch,
        )

    def _apply_tool_batch_state(self, batch_state) -> None:
        state = self.state
        state.session_name = batch_state.session_name
        state.exposed_tools = set(batch_state.exposed_tools)
        state.rejected_tool_signature = batch_state.rejected_tool_signature
        state.rejected_repeat = batch_state.rejected_repeat
        state.plan_state = batch_state.plan.plan_state
        state.awaiting_finish = batch_state.plan.awaiting_finish
        state.phase_start_conversation_index = batch_state.plan.phase_start_convo_index
        state.phase_started_at = batch_state.plan.phase_started_at
        state.phase_mcp_statuses = batch_state.plan.phase_mcp_statuses
        state.last_batch_signature = batch_state.last_batch_signature
        state.consecutive_same_batch = batch_state.consecutive_same_batch

    def inject_flow_directive(self, conversation: list[dict]) -> None:
        append_plan_directive(
            conversation,
            self.session,
            self.state.plan_state,
            awaiting_finish=self.state.awaiting_finish,
        )

    def auto_finalize_plan(self, final_phase_since_ts: float) -> None:
        state = self.state
        if state.plan_state is None:
            return
        request = self.request
        finalize_plan(
            PlanFinalizeContext(
                session=self.session,
                user_id=request.user_id,
                ai_config_id=int(request.ai_config_id),
                ai_kind=request.ai_kind,
                session_id=request.session_id,
                session_name=state.session_name,
                model=self.setup.model,
                task_job=state.task_job,
            ),
            state.plan_state,
            final_phase_since_ts=final_phase_since_ts,
        )
        state.plan_state = None
        state.awaiting_finish = False

    def step_label(self) -> str:
        limit = (
            "todo"
            if has_active_todo_plan(self.state.plan_state)
            else self.setup.max_steps
        )
        return f"{self.state.completed_steps}/{limit}"

    def should_stop(self) -> bool:
        return _run_should_stop(self.request.run_id)

    def stop_run(self) -> None:
        _run_set_status(self.request.run_id, "stopped", finished=True)

    def complete_run(self) -> None:
        _run_set_status(self.request.run_id, "completed", finished=True)

    def set_run_error(self, error: str) -> None:
        _run_set_status(self.request.run_id, "error", error, finished=True)

    def set_live_phase(self, phase: str, tool: str = "") -> None:
        _set_run_live_phase(self.request.run_id, phase, tool)

    def clear_live_text(self) -> None:
        _set_run_live_text(self.request.run_id, "")

    def reset_live_usage(self) -> None:
        _set_run_live_usage(self.request.run_id, 0, 0, 0)

    def _save_step_limit_notice(self) -> None:
        request = self.request
        _save_message(
            self.session,
            request.user_id,
            ChatMessageCreate(
                role="system",
                content=(
                    "[系统提示]\n"
                    f"本轮已达到 MCP 连续执行步数上限（{self.setup.max_steps}）。"
                    "系统已暂停本轮自动继续，避免无限循环；如需继续，请发送新消息"
                    "或提高系统设置里的 MCP 最大步数。"
                ),
                tags="system_notice_mcp_max_steps",
                ai_config_id=request.ai_config_id,
                ai_kind=request.ai_kind,
                session_id=request.session_id,
                session_name=self.state.session_name,
                model=self.setup.model,
                total_tokens=0,
            ),
        )
