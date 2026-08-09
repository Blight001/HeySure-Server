"""Guard and execute one model-produced batch of tool calls."""

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from sqlmodel import Session

from api.models import ChatMessageCreate
from api.services.chat.chat_persistence import _save_message
from ai_runtime.inference.tool_resolution import (
    TurnCallAction,
    append_pending_call_responses,
)


NO_PROGRESS_NOTE = (
    "[系统提示] 检测到你连续多步发出了完全相同的工具调用（相同工具名与参数），"
    "但没有产生新进展。相同的调用不会返回不同的结果——请直接基于上方已有的"
    "工具结果继续推进，或明确给出结论，不要再重复相同的调用与相同的思考。"
)


class ProgressAction(Enum):
    EXECUTE_BATCH = "execute_batch"
    NEXT_TURN = "next_turn"
    STOP_RUN = "stop_run"


@dataclass(frozen=True)
class ProgressContext:
    session: Session
    conversation: List[Dict]
    user_id: int
    ai_config_id: Optional[int]
    ai_kind: str
    session_id: str
    session_name: str
    model: str
    native_tool_calls: bool
    set_live_phase: Callable[[str], None]


@dataclass(frozen=True)
class ProgressState:
    last_batch_signature: str
    consecutive_same_batch: int


@dataclass(frozen=True)
class ProgressOutcome:
    action: ProgressAction
    state: ProgressState


def evaluate_progress(
    context: ProgressContext,
    state: ProgressState,
    turn_calls: List[Dict[str, Any]],
) -> ProgressOutcome:
    signature = batch_signature(turn_calls)
    count = (
        state.consecutive_same_batch + 1
        if signature and signature == state.last_batch_signature
        else 1
    )
    next_state = ProgressState(signature, count)
    if count < 2:
        return ProgressOutcome(ProgressAction.EXECUTE_BATCH, next_state)
    if context.native_tool_calls:
        append_pending_call_responses(
            context.conversation,
            turn_calls,
            {"success": False, "error": "no_progress_loop", "note": NO_PROGRESS_NOTE},
            native=True,
        )
    else:
        context.conversation.append({"role": "user", "content": NO_PROGRESS_NOTE})
    if count >= 3:
        _save_stop_notice(context)
        context.set_live_phase("idle")
        return ProgressOutcome(ProgressAction.STOP_RUN, next_state)
    context.set_live_phase("generating")
    return ProgressOutcome(ProgressAction.NEXT_TURN, next_state)


def batch_signature(turn_calls: List[Dict[str, Any]]) -> str:
    return "\n".join(sorted(
        f"{call.get('tool') or ''}|"
        f"{json.dumps(call.get('arguments') or {}, ensure_ascii=False, sort_keys=True)}"
        for call in turn_calls
    ))


def duplicate_call_flags(turn_calls: List[Dict[str, Any]]) -> List[bool]:
    seen: set[str] = set()
    flags: List[bool] = []
    for call in turn_calls:
        signature = (
            f"{call.get('tool') or ''}|"
            f"{json.dumps(call.get('arguments') or {}, ensure_ascii=False, sort_keys=True)}"
        )
        flags.append(signature in seen)
        seen.add(signature)
    return flags


def execute_turn_batch(
    conversation: List[Dict],
    turn_calls: List[Dict[str, Any]],
    native_tool_calls: bool,
    execute_call: Callable[[Dict[str, Any], List[Dict[str, Any]]], TurnCallAction],
    on_duplicate: Callable[[Dict[str, Any]], None],
) -> TurnCallAction:
    action = TurnCallAction.NEXT_CALL
    duplicate_flags = duplicate_call_flags(turn_calls)
    for index, call in enumerate(turn_calls):
        if duplicate_flags[index]:
            on_duplicate(call)
            append_pending_call_responses(
                conversation,
                [call],
                {
                    "success": True,
                    "note": "duplicate_call_merged",
                    "detail": "本轮已执行过完全相同的工具调用（同名同参数），此重复调用未再次执行，结果同上。",
                },
                native=native_tool_calls,
            )
            continue
        action = execute_call(call, turn_calls[index + 1:])
        if action is not TurnCallAction.NEXT_CALL:
            break
    return action


def _save_stop_notice(context) -> None:
    _save_message(
        context.session,
        context.user_id,
        ChatMessageCreate(
            role="system",
            content=(
                "[系统提示]\n"
                "检测到连续多步重复相同的工具调用且无新进展，已自动结束本轮以避免死循环。"
                "如需继续，请发送新消息或调整需求。"
            ),
            tags="system_notice_no_progress_loop",
            ai_config_id=context.ai_config_id,
            ai_kind=context.ai_kind,
            session_id=context.session_id,
            session_name=context.session_name,
            model=context.model,
            total_tokens=0,
        ),
    )
