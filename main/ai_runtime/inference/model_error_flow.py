"""Recover model-request failures without losing tool or image invariants."""

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from sqlmodel import Session

from api.chat_runtime.chat_prompt_utils import _safe_json
from api.models import ChatMessageCreate
from api.services.chat.chat_persistence import _save_message
from ai_runtime.inference import tool_media


@dataclass(frozen=True)
class ModelErrorContext:
    session: Session
    conversation: List[Dict]
    user_id: int
    ai_config_id: Optional[int]
    ai_kind: str
    session_id: str
    session_name: str
    model: str
    set_generating: Callable[[], None]
    set_run_error: Callable[[str], None]


@dataclass(frozen=True)
class ModelErrorDecision:
    consecutive_errors: int
    image_input_disabled: bool
    stop_run: bool


def handle_model_error(
    context: ModelErrorContext,
    error_text: str,
    consecutive_errors: int,
    image_input_disabled: bool,
) -> ModelErrorDecision:
    error_count = consecutive_errors + 1
    repaired_ids = repair_missing_tool_responses(
        context.conversation,
        error_text,
    )
    if repaired_ids:
        _save_context_repair_notice(context, repaired_ids)
        context.set_generating()
        return ModelErrorDecision(0, image_input_disabled, False)
    if tool_media.is_image_input_unsupported_error(error_text):
        removed = tool_media.degrade_image_messages_to_text(context.conversation)
        if removed:
            _save_image_degradation_notice(context, error_text, removed)
            context.set_generating()
            return ModelErrorDecision(0, True, False)
    _save_retry_notice(context, error_text, error_count)
    context.set_generating()
    stop_run = error_count >= 3
    if stop_run:
        context.set_run_error(
            f"AI request failed 3 times consecutively: {error_text}"
        )
    return ModelErrorDecision(error_count, image_input_disabled, stop_run)


def repair_missing_tool_responses(
    conversation: List[Dict],
    error_text: str,
) -> List[str]:
    repaired_ids = []
    index = 0
    while index < len(conversation):
        item = conversation[index]
        if item.get("role") == "tool":
            conversation.pop(index)
            continue
        if item.get("role") != "assistant" or not item.get("tool_calls"):
            index += 1
            continue
        index, repaired = _repair_assistant_calls(
            conversation,
            index,
            error_text,
        )
        repaired_ids.extend(repaired)
    return repaired_ids


def _repair_assistant_calls(conversation, index, error_text):
    expected_ids = [
        str(call.get("id") or "").strip()
        for call in conversation[index].get("tool_calls") or []
        if isinstance(call, dict) and str(call.get("id") or "").strip()
    ]
    if not expected_ids:
        return index + 1, []
    seen_ids = set()
    insert_at = index + 1
    while insert_at < len(conversation) and conversation[insert_at].get("role") == "tool":
        tool_call_id = str(conversation[insert_at].get("tool_call_id") or "").strip()
        if tool_call_id in expected_ids and tool_call_id not in seen_ids:
            seen_ids.add(tool_call_id)
            insert_at += 1
        else:
            conversation.pop(insert_at)
    missing_ids = [item for item in expected_ids if item not in seen_ids]
    for offset, tool_call_id in enumerate(missing_ids):
        conversation.insert(insert_at + offset, {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": _safe_json({
                "success": False,
                "error": error_text,
                "recovered": True,
            }),
        })
    return insert_at + len(missing_ids), missing_ids


def _save_context_repair_notice(context, repaired_ids) -> None:
    _save_notice(
        context,
        "\n".join([
            "[AI 对话上下文已修复]",
            "已补齐缺失的 tool 响应，避免上游接口因 tool_calls 上下文不完整而拒绝请求。",
            f"补齐 tool_call_id: {', '.join(repaired_ids)}",
        ]),
        "system_notice_ai_context_repaired",
    )


def _save_image_degradation_notice(context, error_text, removed) -> None:
    context.conversation.append({
        "role": "user",
        "content": tool_media.image_input_degraded_feedback(error_text, removed),
    })
    _save_notice(
        context,
        "\n".join([
            "[AI 对话出错]",
            error_text,
            "",
            f"检测到当前模型不支持图片输入；系统已移除 {removed} 张图片。",
            "该错误已作为运行时消息发送给 AI，对话将继续执行。",
        ]),
        "system_notice_ai_error",
    )


def _save_retry_notice(context, error_text, error_count) -> None:
    lines = [
        "[AI 对话出错]",
        error_text,
        "",
        f"连续错误次数: {error_count}/3",
    ]
    if error_count < 3:
        lines.extend([
            "",
            "系统将重试上游请求；该错误不会作为 user 消息发送给 AI。",
        ])
    _save_notice(context, "\n".join(lines), "system_notice_ai_error")


def _save_notice(context, content, tags) -> None:
    _save_message(
        context.session,
        context.user_id,
        ChatMessageCreate(
            role="system", content=content, tags=tags,
            ai_config_id=context.ai_config_id, ai_kind=context.ai_kind,
            session_id=context.session_id, session_name=context.session_name,
            model=context.model, total_tokens=0,
        ),
    )
