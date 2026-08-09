"""Build provider conversation history from persisted chat messages."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from api.services.chat import chat_inject, mcp_session_context
from ai_runtime.inference.communication_prompt import should_replay_system_notice_as_user


def build_conversation_history(
    history: Iterable[object],
    *,
    system_prompt: str,
    mcp_result_max_chars: int,
    model_user_content: str | None = None,
) -> list[dict[str, Any]]:
    """Convert persisted rows into a valid provider message sequence.

    Persisted UI-only notices and compacted rows are omitted. Historical MCP
    bubbles are restored as native assistant/tool pairs while keeping a
    preceding assistant message and its ``tool_calls`` adjacent.
    """

    conversation: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt}
    ]
    for message in history:
        _append_history_message(conversation, message, mcp_result_max_chars)

    if model_user_content:
        _replace_latest_user_content(conversation, model_user_content)
    return conversation


def _append_history_message(
    conversation: list[dict[str, Any]],
    message: object,
    mcp_result_max_chars: int,
) -> None:
    tags = str(getattr(message, "tags", "") or "")
    if _skip_history_message(tags):
        return

    role = str(getattr(message, "role", "") or "")
    content = getattr(message, "content", None)
    if role in {"user", "assistant"}:
        # Historical reasoning is intentionally not replayed. Only the visible
        # content is stable context for a later inference run.
        conversation.append({"role": role, "content": content})
        return
    if role != "system":
        return
    if should_replay_system_notice_as_user(tags) or "phase_summary" in tags:
        conversation.append({"role": "user", "content": content})
        return
    if "mcp_tool_call" not in tags or "mode.manage" in str(content or ""):
        return

    compact_pair = mcp_session_context.compact_mcp_history_messages(
        getattr(message, "id", None),
        str(content or ""),
        mcp_result_max_chars,
    )
    if not compact_pair:
        return
    if _can_attach_tool_call(conversation):
        conversation[-1]["tool_calls"] = compact_pair[0]["tool_calls"]
        conversation.append(compact_pair[1])
        return
    conversation.extend(compact_pair)


def _can_attach_tool_call(conversation: list[dict[str, Any]]) -> bool:
    return bool(
        conversation
        and conversation[-1].get("role") == "assistant"
        and not conversation[-1].get("tool_calls")
    )


def _skip_history_message(tags: str) -> bool:
    return (
        "system_notice_ai_error" in tags
        or "system_notice_ai_context_repaired" in tags
        or "compressed_away" in tags
        or chat_inject.PENDING_INJECT_TAG in tags
    )


def _replace_latest_user_content(
    conversation: list[dict[str, Any]], content: str
) -> None:
    for index in range(len(conversation) - 1, -1, -1):
        if conversation[index].get("role") == "user":
            conversation[index] = {"role": "user", "content": content}
            return
