"""Persist and report MCP-disabled and disallowed tool calls."""

from dataclasses import dataclass
import json
from typing import Callable, Dict, List, Optional

from sqlmodel import Session

from api.chat_runtime.chat_prompt_utils import (
    _append_mcp_state_to_tags,
    _build_mcp_display_result,
    _safe_json,
)
from api.models import ChatMessage, ChatMessageCreate
from api.services.chat.chat_persistence import _save_message
from ai_runtime.inference import tool_persistence
from ai_runtime.inference.tool_resolution import (
    TurnCallAction,
    append_pending_call_responses,
    track_repeated_tool_call,
)


@dataclass(frozen=True)
class RejectionContext:
    session: Session
    conversation: List[Dict]
    pending: List[Dict]
    saved_message: ChatMessage
    user_id: int
    ai_config_id: Optional[int]
    ai_kind: str
    session_id: str
    session_name: str
    model: str
    run_id: str
    native_tool_calls: bool
    set_live_phase: Callable[[str], None]
    set_run_error: Callable[[str], None]


@dataclass(frozen=True)
class RejectionOutcome:
    signature: str
    count: int
    action: TurnCallAction


@dataclass(frozen=True)
class ToolResolutionInfo:
    raw_tool: str = ""
    known_tools: frozenset[str] = frozenset()


def handle_mcp_disabled(
    context: RejectionContext,
    tool: str,
    arguments: dict,
    call_id: str,
    previous_signature: str,
    previous_count: int,
) -> RejectionOutcome:
    signature, count = track_repeated_tool_call(
        "mcp_disabled",
        tool,
        arguments,
        previous_signature,
        previous_count,
    )
    _append_disabled_feedback(context, tool, arguments, call_id)
    append_pending_call_responses(
        context.conversation,
        context.pending,
        {"success": False, "error": "MCP is disabled for this AI"},
        native=context.native_tool_calls,
    )
    if count >= 3:
        context.set_run_error("Repeated MCP call while MCP is disabled")
        return RejectionOutcome(signature, count, TurnCallAction.STOP_RUN)
    context.set_live_phase("generating")
    return RejectionOutcome(signature, count, TurnCallAction.NEXT_TURN)


def _append_disabled_feedback(context, tool, arguments, call_id) -> None:
    tool_name = str(tool or "").strip() or "unknown"
    payload = {
        "success": False,
        "error": "MCP is disabled for this AI",
        "tool": tool_name,
        "arguments": arguments or {},
        "instruction": (
            "The requested MCP call was not executed because MCP is disabled or not effective "
            "for this AI. Do not wait for a tool result. Continue by explaining the limitation "
            "to the user, asking them to enable MCP if tool execution is required, or completing "
            "the task without MCP when possible."
        ),
    }
    notice = (
        "[系统提示] 检测到 MCP 调用未生效。\n"
        f"- 工具: {tool_name}\n"
        "- 原因: 当前 AI 的 MCP 开关关闭或 MCP 未生效，系统没有执行该工具。\n\n"
        "请不要停在等待 MCP 结果的状态；请继续回复用户，说明无法执行该 MCP，"
        "必要时请用户开启 MCP 或改用无需 MCP 的方式完成。"
    )
    _save_message(
        context.session,
        context.user_id,
        ChatMessageCreate(
            role="user", content=notice, tags="system_notice_mcp_disabled",
            ai_config_id=context.ai_config_id, ai_kind=context.ai_kind,
            session_id=context.session_id, session_name=context.session_name,
            model=context.model, total_tokens=0,
        ),
    )
    if context.native_tool_calls:
        context.conversation.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": _safe_json(payload),
        })
    else:
        context.conversation.append({
            "role": "user",
            "content": f"{notice}\n\n[工具检查结果]\n{_safe_json(payload)}",
        })


def handle_disallowed_tool(
    context: RejectionContext,
    tool: str,
    arguments: dict,
    call_id: str,
    allowed_tools: set,
    previous_signature: str,
    previous_count: int,
    resolution: Optional[ToolResolutionInfo] = None,
) -> RejectionOutcome:
    signature, count = track_repeated_tool_call(
        "disallowed",
        tool,
        arguments,
        previous_signature,
        previous_count,
    )
    info = resolution or ToolResolutionInfo()
    unknown = tool not in info.known_tools
    requested = str(info.raw_tool or tool)
    error = (
        f"Unknown MCP tool name: {requested}. This is a tool-name compatibility error, not a permission denial."
        if unknown else f"Tool not allowed for this task: {tool}"
    )
    result = {"result": {"success": False, "error": error}}
    result_text = _build_mcp_display_result(
        tool,
        result,
        ok=False,
        error_message=error,
    )
    context.saved_message.tags = _append_mcp_state_to_tags(
        context.saved_message.tags,
        tool,
        arguments,
        result_text,
    )
    context.session.add(context.saved_message)
    context.session.commit()
    tool_persistence.save_tool_bubble(tool_persistence.ToolBubbleRequest(
        session=context.session, user_id=context.user_id,
        ai_config_id=context.ai_config_id, ai_kind=context.ai_kind,
        session_id=context.session_id, session_name=context.session_name,
        model=context.model, tool=tool, arguments=arguments,
        result_text=result_text, failed=True,
    ))
    _append_disallowed_response(context, tool, error, call_id, allowed_tools, unknown=unknown)
    if count >= 3:
        append_pending_call_responses(
            context.conversation,
            context.pending,
            {"success": False, "error": "Run aborted: repeated disallowed MCP tool call"},
            native=context.native_tool_calls,
        )
        context.set_run_error(f"Repeated disallowed MCP tool call: {tool}")
        return RejectionOutcome(signature, count, TurnCallAction.STOP_RUN)
    return RejectionOutcome(signature, count, TurnCallAction.NEXT_CALL)


def _append_disallowed_response(context, tool, error, call_id, allowed_tools, *, unknown=False) -> None:
    if context.native_tool_calls:
        context.conversation.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(
                {"error": error, "allowed_tools": sorted(allowed_tools)},
                ensure_ascii=False,
            ),
        })
        return
    explanation = (
        f"工具名 `{tool}` 无法对应当前已注册工具；这是名称或格式错误，不代表 MCP 权限被关闭。"
        if unknown else f"工具 `{tool}` 已知，但未在当前任务允许范围内。"
    )
    context.conversation.append({"role": "user", "content": (
        "[MCP执行失败]\n"
        f"{explanation}\n"
        f"可用工具: {', '.join(sorted(allowed_tools)) or '（空）'}\n"
        "请使用规范工具名或改用当前任务允许的 MCP 工具继续执行。"
    )})
