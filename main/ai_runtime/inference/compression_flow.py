"""Manual and threshold-triggered conversation compression transitions."""

from dataclasses import dataclass
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from sqlmodel import Session

from api.chat_runtime.chat_runtime_helpers import _session_total_tokens
from api.chat_runtime.chat_prompt_utils import _safe_json
from api.services.chat import conversation_compress
from ai_runtime.inference import tool_persistence
from ai_runtime.inference.tool_resolution import append_pending_call_responses


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CompressionContext:
    session: Session
    user: object
    config: object
    user_id: int
    ai_config_id: Optional[int]
    ai_kind: str
    session_id: str
    session_name: str
    model: str
    api_key: str
    base_url: str
    system_prompt: str
    compression_prompt: str
    plan_state: object
    reset_live_usage: Callable[[], None]
    set_generating: Callable[[], None]
    inject_flow_directive: Callable[[List[Dict]], None]


@dataclass(frozen=True)
class CompressionState:
    conversation: List[Dict]
    compression_failed: bool
    phase_start_convo_index: int
    phase_started_at: float
    phase_mcp_statuses: List[tuple]


@dataclass(frozen=True)
class CompressionDecision:
    handled: bool
    continue_loop: bool
    state: CompressionState


def handle_manual_compression(
    context: CompressionContext,
    state: CompressionState,
    turn_calls: List[Dict[str, Any]],
    native_tool_calls: bool,
) -> CompressionDecision:
    compress_call = next(
        (
            call
            for call in turn_calls
            if call["tool"] == "conversation.manage"
            and str((call["arguments"] or {}).get("action") or "").strip().lower()
            == "compress"
        ),
        None,
    )
    if compress_call is None:
        return CompressionDecision(False, False, state)
    keep_recent = _bounded_keep_recent(compress_call.get("arguments") or {})
    rebuilt = _compress(
        context,
        state.conversation,
        0,
        0,
        keep_recent,
        on_tool_result=_tool_result_callback(
            context,
            compress_call.get("arguments") or {},
        ),
    )
    next_state = state
    if rebuilt:
        rebuilt.append({
            "role": "user",
            "content": (
                "已完成上下文压缩；详细摘要已写入对话记录。"
                "请严格继承摘要中的目标、约束、进度、关键数据、待办与风险继续执行。"
            ),
        })
        context.reset_live_usage()
        next_state = _reanchor_state(state, rebuilt, context.plan_state is not None)
    else:
        _append_manual_failure(
            state.conversation,
            turn_calls,
            compress_call,
            native_tool_calls,
        )
    context.set_generating()
    return CompressionDecision(True, True, next_state)


def _bounded_keep_recent(arguments: dict) -> int:
    try:
        keep_recent = int(arguments.get("keep_recent", 4))
    except (TypeError, ValueError):
        keep_recent = 4
    return max(0, min(keep_recent, 20))


def _tool_result_callback(context: CompressionContext, arguments: dict):
    started_at = time.perf_counter()

    def persist_tool_result(success: bool, result_text: str) -> None:
        tool_persistence.save_tool_bubble(tool_persistence.ToolBubbleRequest(
            session=context.session,
            user_id=context.user_id,
            ai_config_id=context.ai_config_id,
            ai_kind=context.ai_kind,
            session_id=context.session_id,
            session_name=context.session_name,
            model=context.model,
            tool="conversation.manage",
            arguments=arguments,
            result_text=result_text,
            failed=not success,
            latency=max(0.0, time.perf_counter() - started_at),
        ))

    return persist_tool_result


def _compress(
    context,
    conversation,
    session_tokens,
    threshold,
    keep_recent=None,
    on_tool_result=None,
):
    request = conversation_compress.CompressionRequest(
        convo=conversation,
        user_id=context.user_id,
        ai_config_id=context.ai_config_id,
        ai_kind=context.ai_kind,
        session_id=context.session_id,
        session_name=context.session_name,
        model=context.model,
        api_key=context.api_key,
        base_url=context.base_url,
        system_prompt=context.system_prompt,
        compression_prompt=context.compression_prompt,
        session_tokens=session_tokens,
        threshold=threshold,
        keep_recent=4 if keep_recent is None else keep_recent,
        on_tool_result=on_tool_result,
    )
    try:
        return conversation_compress.compress_session(context.session, request)
    except Exception:
        logger.exception("conversation compression failed")
        return None


def _append_manual_failure(conversation, turn_calls, compress_call, native) -> None:
    note = "上下文压缩未完成，原始消息已保留，请继续当前对话。"
    if not native:
        conversation.append({"role": "user", "content": note})
        return
    conversation.append({
        "role": "tool",
        "tool_call_id": compress_call["id"],
        "content": _safe_json({
            "success": False,
            "compressed": False,
            "error": "compression_failed",
            "note": note,
        }),
    })
    append_pending_call_responses(
        conversation,
        [call for call in turn_calls if call is not compress_call],
        {
            "success": False,
            "error": "not_executed",
            "note": (
                "conversation.manage(compress) rewrites the whole context, "
                "so it runs on its own. Re-issue this call afterwards."
            ),
        },
        native=True,
    )


def maybe_auto_compress(
    context: CompressionContext,
    state: CompressionState,
    turn_calls: List[Dict[str, Any]],
    task_is_finished: bool,
) -> CompressionDecision:
    if not _auto_compression_allowed(context, state, turn_calls, task_is_finished):
        return CompressionDecision(False, False, state)
    threshold = max(1, int(getattr(context.config, "token_limit", 0) or 1))
    session_tokens = _session_total_tokens(
        context.session,
        context.user_id,
        context.ai_kind,
        context.session_id,
        context.ai_config_id,
    )
    if session_tokens < threshold:
        return CompressionDecision(False, False, state)
    rebuilt = _compress(
        context,
        state.conversation,
        session_tokens,
        threshold,
        on_tool_result=_tool_result_callback(
            context,
            {"action": "compress", "trigger": "auto"},
        ),
    )
    if not rebuilt:
        return CompressionDecision(
            True,
            False,
            CompressionState(
                conversation=state.conversation,
                compression_failed=True,
                phase_start_convo_index=state.phase_start_convo_index,
                phase_started_at=state.phase_started_at,
                phase_mcp_statuses=state.phase_mcp_statuses,
            ),
        )
    context.reset_live_usage()
    next_state = _reanchor_state(state, rebuilt, context.plan_state is not None)
    if context.plan_state is not None:
        context.inject_flow_directive(rebuilt)
    return CompressionDecision(True, True, next_state)


def _auto_compression_allowed(context, state, turn_calls, task_is_finished) -> bool:
    return bool(
        context.config
        and getattr(context.config, "ai_role", "") == "digital_member"
        and bool(getattr(context.user, "conversation_auto_compress_enabled", True))
        and not task_is_finished
        and not state.compression_failed
        and not any(call["tool"] == "todo.manage" for call in turn_calls)
    )


def _reanchor_state(state, conversation, plan_active) -> CompressionState:
    if not plan_active:
        return CompressionState(
            conversation=conversation,
            compression_failed=state.compression_failed,
            phase_start_convo_index=state.phase_start_convo_index,
            phase_started_at=state.phase_started_at,
            phase_mcp_statuses=state.phase_mcp_statuses,
        )
    return CompressionState(
        conversation=conversation,
        compression_failed=state.compression_failed,
        phase_start_convo_index=len(conversation),
        phase_started_at=time.time(),
        phase_mcp_statuses=[],
    )
