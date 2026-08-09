"""Normalize and persist one successful inference turn."""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sqlmodel import Session

from api.chat_runtime.chat_prompt_utils import _safe_json
from api.models import ChatMessage, ChatMessageCreate
from api.services.chat.chat_persistence import _save_message
from ai_runtime.inference.tool_resolution import resolve_mcp_tool_name


@dataclass(frozen=True)
class AssistantTurnContext:
    session: Session
    conversation: List[Dict]
    user_id: int
    ai_config_id: Optional[int]
    ai_kind: str
    session_id: str
    session_name: str
    model: str
    system_prompt: str
    native_tool_name_map: Dict[str, str]
    allowed_tools: frozenset[str]


@dataclass(frozen=True)
class PersistedAssistantTurn:
    saved_message: ChatMessage
    tool_calls: List[Dict[str, Any]]
    conversation_start: int
    token_triplet: str


def persist_assistant_turn(
    context: AssistantTurnContext,
    stream_result,
    latency: float,
) -> PersistedAssistantTurn:
    tool_calls = _resolve_tool_calls(
        stream_result.tool_calls,
        context.native_tool_name_map,
        context.allowed_tools,
    )
    usage = stream_result.usage
    saved = _save_message(
        context.session,
        context.user_id,
        ChatMessageCreate(
            role="assistant",
            content=stream_result.assistant_text,
            think=stream_result.reasoning_content or None,
            tags="mcp_assistant_call" if tool_calls else "",
            ai_config_id=context.ai_config_id,
            ai_kind=context.ai_kind,
            session_id=context.session_id,
            session_name=context.session_name,
            model=context.model,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
            cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0) or None,
            system_prompt=context.system_prompt,
            finish_reason=stream_result.finish_reason,
            latency=latency,
        ),
    )
    conversation_start = len(context.conversation)
    context.conversation.append(_assistant_conversation_item(stream_result, tool_calls))
    return PersistedAssistantTurn(
        saved_message=saved,
        tool_calls=tool_calls,
        conversation_start=conversation_start,
        token_triplet=_token_triplet(usage),
    )


def _resolve_tool_calls(raw_calls, native_name_map, allowed_tools):
    resolved = []
    for raw_call in raw_calls:
        call = dict(raw_call)
        call["tool"] = resolve_mcp_tool_name(
            raw_call.get("tool", ""),
            native_name_map,
            allowed_tools,
        )
        resolved.append(call)
    return resolved


def _assistant_conversation_item(stream_result, tool_calls):
    if stream_result.has_native_tc and tool_calls:
        item = {
            "role": "assistant",
            "content": stream_result.assistant_text or None,
            "tool_calls": [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["native_name"] or call["tool"],
                        "arguments": call["raw_arguments"] or _safe_json(call["arguments"]),
                    },
                }
                for call in tool_calls
            ],
        }
    else:
        item = {"role": "assistant", "content": stream_result.assistant_text}
    if stream_result.reasoning_content:
        item["reasoning_content"] = stream_result.reasoning_content
    return item


def _token_triplet(usage) -> str:
    return (
        f"{int(usage.get('prompt_tokens') or 0)}/"
        f"{int(usage.get('completion_tokens') or 0)}/"
        f"{int(usage.get('total_tokens') or 0)}"
    )
