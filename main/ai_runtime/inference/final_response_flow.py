"""Finalize an inference turn that produced no tool calls."""

import logging
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from sqlmodel import Session

from api.chat_runtime.chat_prompt_utils import (
    _append_mcp_state_to_tags,
    _build_mcp_stream_warning,
)
from api.chat_runtime.chat_runtime_helpers import _renew_loop_scheduled_job
from api.models import ChatMessage, ChatMessageCreate
from api.services.chat import chat_inject
from api.services.chat.chat_persistence import _save_message
from api.services.tasks import task_plan as plan_service
from ai_runtime.inference import ai_message_service
from ai_runtime.inference.policies import has_active_todo_plan


logger = logging.getLogger(__name__)


class FinalResponseAction(Enum):
    NEXT_TURN = "next_turn"
    COMPLETE_RUN = "complete_run"


@dataclass(frozen=True)
class FinalResponseContext:
    session: Session
    conversation: List[Dict]
    saved_message: ChatMessage
    user_id: int
    ai_config_id: Optional[int]
    ai_kind: str
    session_id: str
    session_name: str
    model: str
    config: Any
    warning_template: str
    assistant_text: str
    native_tool_calls: bool
    phase_started_at: float
    set_live_phase: Callable[[str], None]
    auto_finalize_plan: Callable[[float], None]
    notify_task_completion: Callable[..., None]


@dataclass(frozen=True)
class FinalResponseState:
    markup_fallback_available: bool
    pending_ai_reply_message_id: str
    plan_state: Any
    awaiting_finish: bool
    task_job: Any


@dataclass(frozen=True)
class FinalResponseOutcome:
    action: FinalResponseAction
    state: FinalResponseState


def handle_final_response(
    context: FinalResponseContext,
    state: FinalResponseState,
) -> FinalResponseOutcome:
    warning = _format_warning(context, state.markup_fallback_available)
    if warning:
        _persist_warning(context, warning)
        return FinalResponseOutcome(
            FinalResponseAction.NEXT_TURN,
            replace(state, markup_fallback_available=False),
        )
    injects = _drain_pending_injects(context)
    if injects:
        context.conversation.extend(
            {"role": "user", "content": text} for text in injects
        )
        context.set_live_phase("generating")
        return FinalResponseOutcome(FinalResponseAction.NEXT_TURN, state)
    state = replace(
        state,
        pending_ai_reply_message_id=_complete_inbound_reply(
            context,
            state.pending_ai_reply_message_id,
        ),
    )
    state = _reload_plan(context, state)
    if has_active_todo_plan(state.plan_state):
        return _finish_or_continue_plan(context, state)
    _finalize_simple_task(context, state.task_job)
    return FinalResponseOutcome(FinalResponseAction.COMPLETE_RUN, state)


def _format_warning(context, fallback_available):
    if context.native_tool_calls:
        return ""
    return _build_mcp_stream_warning(
        context.assistant_text,
        context.config,
        context.warning_template,
        markup_fallback=fallback_available,
    )


def _persist_warning(context, warning) -> None:
    _save_message(
        context.session,
        context.user_id,
        ChatMessageCreate(
            role="user", content=warning, tags="system_notice_mcp_format_invalid",
            ai_config_id=context.ai_config_id, ai_kind=context.ai_kind,
            session_id=context.session_id, session_name=context.session_name,
            model=context.model, total_tokens=0,
        ),
    )
    context.conversation.append({"role": "user", "content": warning})


def _drain_pending_injects(context):
    try:
        return chat_inject.pop_pending_injects(
            context.user_id,
            context.ai_config_id,
            context.ai_kind,
            context.session_id,
        )
    except Exception:
        logger.exception("final pending user-inject drain failed")
        return []


def _complete_inbound_reply(context, pending_message_id):
    if not pending_message_id or not context.assistant_text.strip():
        return pending_message_id
    if context.ai_config_id is None:
        return pending_message_id
    try:
        reply = ai_message_service.complete_inbound_with_assistant_reply(
            message_id=pending_message_id,
            user_id=context.user_id,
            replier_ai_config_id=int(context.ai_config_id),
            content=context.assistant_text,
        )
        if reply and reply.get("auto_completed"):
            context.saved_message.tags = _append_mcp_state_to_tags(
                context.saved_message.tags,
                "ai.auto_reply",
                {"message_id": pending_message_id},
                "assistant final text delivered as AI message reply",
            )
            context.session.add(context.saved_message)
            context.session.commit()
    except Exception:
        logger.exception("auto AI message reply failed")
    return ""


def _reload_plan(context, state):
    if context.ai_config_id is None:
        return state
    try:
        plan = plan_service.get_active_plan(
            context.session,
            context.user_id,
            int(context.ai_config_id),
            context.session_id,
        )
        awaiting = plan_service.awaiting_finish(context.session, plan)
        return replace(state, plan_state=plan, awaiting_finish=awaiting)
    except Exception:
        logger.exception("plan reload after natural model stop failed")
        return state


def _finish_or_continue_plan(context, state):
    if state.awaiting_finish:
        context.auto_finalize_plan(context.phase_started_at)
        context.set_live_phase("idle")
        return FinalResponseOutcome(
            FinalResponseAction.COMPLETE_RUN,
            replace(state, plan_state=None, awaiting_finish=False),
        )
    context.set_live_phase("generating")
    return FinalResponseOutcome(FinalResponseAction.NEXT_TURN, state)


def _finalize_simple_task(context, task_job) -> None:
    terminal = {"completed", "cancelled", "stopped", "error"}
    if task_job is None or str(getattr(task_job, "status", "") or "").strip() in terminal:
        return
    try:
        active_plan = _active_plan_for_task(context)
        if active_plan is not None:
            return
        finished_at = time.time()
        _notify_simple_task_completion(context, task_job)
        renewed = _renew_simple_loop(context, task_job, finished_at)
        if renewed is None:
            task_job.status = "completed"
            task_job.finished_at = finished_at
            task_job.updated_at = finished_at
            context.session.add(task_job)
        try:
            context.session.commit()
        except Exception:
            pass
    except Exception:
        logger.exception("auto finalize simple task job failed")


def _active_plan_for_task(context):
    if context.ai_config_id is None:
        return None
    return plan_service.get_active_plan(
        context.session,
        context.user_id,
        int(context.ai_config_id),
        context.session_id,
    )


def _notify_simple_task_completion(context, task_job) -> None:
    try:
        context.notify_task_completion(
            user_id=context.user_id,
            job_id=str(task_job.job_id or ""),
            summary="任务执行完成（简单任务，无计划流程）。",
        )
    except Exception:
        logger.exception("auto simple task completion notify failed")


def _renew_simple_loop(context, task_job, finished_at):
    try:
        return _renew_loop_scheduled_job(context.session, task_job, finished_at)
    except Exception:
        logger.exception("auto simple task loop schedule failed")
        return None
