"""Conversation-rewriting tool transitions for planned inference runs."""

from dataclasses import dataclass
import logging
import time
from typing import Callable, Dict, List, Optional

from sqlmodel import Session

from api.chat_runtime.chat_prompt_utils import _safe_json
from api.models import ChatMessage, ChatMessageCreate, TaskPlan
from api.services.chat.chat_persistence import _save_message
from api.services.tasks import task_plan as plan_service
from ai_runtime.inference import phase_context, tool_media
from ai_runtime.inference.tool_resolution import (
    TurnCallAction,
    append_control_tool_result,
    append_pending_call_responses,
    flush_screenshot_messages,
    infer_todo_action,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlanFlowSnapshot:
    plan_state: Optional[TaskPlan]
    awaiting_finish: bool
    phase_start_convo_index: int
    phase_started_at: float
    phase_mcp_statuses: List[tuple]


@dataclass(frozen=True)
class PlanTransitionContext:
    session: Session
    conversation: List[Dict]
    pending: List[Dict]
    screenshot_messages: List[Dict]
    user_id: int
    ai_config_id: Optional[int]
    ai_kind: str
    session_id: str
    session_name: str
    model: str
    native_tool_calls: bool
    system_prompt: str
    current_user_message_id: Optional[int]
    model_user_content: Optional[str]
    set_live_phase: Callable[[str], None]
    complete_run: Callable[[], None]
    auto_finalize_plan: Callable[[float], None]


@dataclass(frozen=True)
class ControlToolCall:
    tool: str
    arguments: dict
    tool_result: Dict[str, object]
    failed: bool
    call_id: str


@dataclass(frozen=True)
class PlanTransition:
    action: TurnCallAction
    snapshot: PlanFlowSnapshot


def handle_plan_transition(
    context: PlanTransitionContext,
    snapshot: PlanFlowSnapshot,
    call: ControlToolCall,
) -> Optional[PlanTransition]:
    if call.failed:
        return None
    result_payload = call.tool_result.get("result", call.tool_result)
    if _is_current_session_clear(context, call, result_payload):
        return _handle_clear(context, snapshot, call)
    if call.tool != "todo.manage":
        return None
    action = infer_todo_action(call.arguments)
    if action == "create":
        return _handle_create(context, snapshot, call)
    if action == "edit" and snapshot.plan_state is not None:
        return _handle_edit(context, snapshot, call, result_payload)
    if action == "delete":
        return _handle_delete(context, snapshot, call)
    return None


def _is_current_session_clear(context, call, result_payload) -> bool:
    return (
        call.tool == "conversation.manage"
        and isinstance(result_payload, dict)
        and result_payload.get("action") == "clear"
        and str(result_payload.get("session_id") or "") == str(context.session_id)
    )


def _load_current_user_content(context: PlanTransitionContext) -> str:
    fallback = str(context.model_user_content or "").strip()
    if not context.current_user_message_id:
        return fallback
    row = context.session.get(ChatMessage, context.current_user_message_id)
    if not row:
        return fallback
    matches = (
        row.user_id == context.user_id
        and row.ai_config_id == context.ai_config_id
        and row.ai_kind == context.ai_kind
        and row.session_id == context.session_id
        and row.role == "user"
    )
    return fallback or str(row.content or "").strip() if matches else fallback


def _reset_after_clear(context, tool_result) -> None:
    result_payload = tool_result.get("result", tool_result)
    follow_up = (
        "[MCP执行结果]\n"
        "系统已执行工具：conversation.manage（action=clear）\n"
        "执行状态：成功\n\n"
        "[工具执行结果]\n"
        f"{_safe_json(result_payload)}\n\n"
        "旧上下文已从本轮模型上下文中移除。请只基于当前用户消息和以上结果继续。"
    )
    context.conversation.clear()
    context.conversation.append({"role": "system", "content": context.system_prompt})
    current_content = _load_current_user_content(context)
    if current_content:
        context.conversation.append({"role": "user", "content": current_content})
    context.conversation.append({"role": "user", "content": follow_up})


def _handle_clear(context, snapshot, call) -> PlanTransition:
    context.screenshot_messages.clear()
    _reset_after_clear(context, call.tool_result)
    next_snapshot = snapshot
    if snapshot.plan_state is not None:
        next_snapshot = PlanFlowSnapshot(
            plan_state=snapshot.plan_state,
            awaiting_finish=snapshot.awaiting_finish,
            phase_start_convo_index=len(context.conversation),
            phase_started_at=time.time(),
            phase_mcp_statuses=[],
        )
    context.set_live_phase("generating")
    return PlanTransition(TurnCallAction.NEXT_TURN, next_snapshot)


def _append_control_result(context, call) -> None:
    append_control_tool_result(
        context.conversation,
        call.tool,
        tool_media.model_visible_tool_result(
            call.tool,
            call.tool_result,
            image_attached=False,
        ),
        call.call_id,
        native=context.native_tool_calls,
    )


def _close_pending(context, note: str) -> None:
    append_pending_call_responses(
        context.conversation,
        context.pending,
        {"success": False, "error": "not_executed", "note": note},
        native=context.native_tool_calls,
    )
    flush_screenshot_messages(context.conversation, context.screenshot_messages)


def _reload_plan(context) -> tuple[Optional[TaskPlan], bool]:
    try:
        plan = (
            plan_service.get_active_plan(
                context.session,
                context.user_id,
                int(context.ai_config_id),
                context.session_id,
            )
            if context.ai_config_id is not None
            else None
        )
        return plan, plan_service.awaiting_finish(context.session, plan)
    except Exception:
        logger.exception("plan reload after todo.manage transition failed")
        return None, False


def _append_current_phase(context, plan) -> None:
    if plan is None:
        return
    progress = plan_service.plan_progress(context.session, plan)
    current = next(
        (
            phase
            for phase in progress["phases"]
            if phase["seq"] == plan.current_phase_seq
        ),
        None,
    )
    context.conversation.append({
        "role": "user",
        "content": phase_context.render_phase_directive(
            current,
            progress["phase_count"],
        ),
    })


def _handle_create(context, snapshot, call) -> PlanTransition:
    _append_control_result(context, call)
    _close_pending(
        context,
        "A todo plan was just created; the system handed over phase 1. "
        "Re-issue this call if the new phase still needs it.",
    )
    plan, awaiting_finish = _reload_plan(context)
    next_snapshot = PlanFlowSnapshot(
        plan_state=plan,
        awaiting_finish=awaiting_finish,
        phase_start_convo_index=len(context.conversation),
        phase_started_at=time.time(),
        phase_mcp_statuses=[],
    )
    if plan is not None and not awaiting_finish:
        _append_current_phase(context, plan)
    context.set_live_phase("generating")
    return PlanTransition(TurnCallAction.NEXT_TURN, next_snapshot)


def _persist_phase_compaction(context, snapshot, text, until_ts) -> None:
    try:
        phase_context.mark_phase_messages_compressed(
            context.session,
            user_id=context.user_id,
            ai_config_id=context.ai_config_id,
            ai_kind=context.ai_kind,
            session_id=context.session_id,
            since_ts=snapshot.phase_started_at,
            until_ts=until_ts,
        )
        _save_message(
            context.session,
            context.user_id,
            ChatMessageCreate(
                role="system",
                content=text,
                tags="phase_summary",
                ai_config_id=context.ai_config_id,
                ai_kind=context.ai_kind,
                session_id=context.session_id,
                session_name=context.session_name,
                model=context.model,
                total_tokens=max(1, len(text) // 3),
            ),
        )
        context.session.commit()
    except Exception:
        logger.exception("phase compaction persistence failed")
        context.session.rollback()


def _handle_edit(context, snapshot, call, result_payload) -> PlanTransition:
    finished_phase = (
        result_payload.get("finished_phase")
        if isinstance(result_payload, dict)
        else None
    )
    boundary = max(
        0,
        min(snapshot.phase_start_convo_index, len(context.conversation)),
    )
    compaction_text = phase_context.build_phase_compaction_text(
        finished_phase,
        snapshot.phase_mcp_statuses,
    )
    context.screenshot_messages.clear()
    context.conversation[boundary:] = [{"role": "user", "content": compaction_text}]
    now = time.time()
    _persist_phase_compaction(context, snapshot, compaction_text, now)
    plan, awaiting_finish = _reload_plan(context)
    next_snapshot = PlanFlowSnapshot(
        plan_state=plan,
        awaiting_finish=awaiting_finish,
        phase_start_convo_index=len(context.conversation),
        phase_started_at=time.time(),
        phase_mcp_statuses=[],
    )
    if awaiting_finish:
        context.auto_finalize_plan(next_snapshot.phase_started_at)
        context.set_live_phase("idle")
        context.complete_run()
        return PlanTransition(TurnCallAction.STOP_RUN, next_snapshot)
    _append_current_phase(context, plan)
    context.set_live_phase("generating")
    return PlanTransition(TurnCallAction.NEXT_TURN, next_snapshot)


def _handle_delete(context, snapshot, call) -> PlanTransition:
    _append_control_result(context, call)
    _close_pending(
        context,
        "The todo plan was just deleted; the flow reset. "
        "Re-issue this call if it is still needed.",
    )
    next_snapshot = PlanFlowSnapshot(
        plan_state=None,
        awaiting_finish=False,
        phase_start_convo_index=snapshot.phase_start_convo_index,
        phase_started_at=snapshot.phase_started_at,
        phase_mcp_statuses=[],
    )
    context.set_live_phase("generating")
    return PlanTransition(TurnCallAction.NEXT_TURN, next_snapshot)
