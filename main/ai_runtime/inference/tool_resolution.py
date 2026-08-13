"""Native provider tool-name encoding, aliases, and safe joined-call checks."""

import copy
from dataclasses import dataclass
import json
import re
from enum import Enum
from typing import Any, Dict, List, Optional

from api.chat_runtime.chat_prompt_utils import _safe_json
from ai_runtime.inference import tool_media
from connector_runtime.dispatch.desktop_device_tools import (
    build_endpoint_tools_payload,
    connected_endpoint_tool_catalog,
    is_endpoint_agent_tool,
)
from mcp_runtime.mcp import registry


class TurnCallAction(str, Enum):
    """State transition requested after one tool call in a model turn."""

    NEXT_CALL = "next_call"
    NEXT_TURN = "next_turn"
    STOP_RUN = "stop_run"


@dataclass(frozen=True)
class ToolResponseContext:
    conversation: List[Dict[str, Any]]
    screenshot_messages: List[Dict[str, Any]]
    turn_convo_start: int
    image_input_disabled: bool
    native_tool_calls: bool


def append_ordinary_tool_response(
    context: ToolResponseContext,
    tool: str,
    arguments: dict,
    tool_result: Dict[str, object],
    failed: bool,
    call_id: str,
) -> None:
    screenshot_message = tool_media.tool_image_message(tool, tool_result)
    attach_screenshot = bool(screenshot_message) and not context.image_input_disabled
    if attach_screenshot:
        tool_media.prune_prior_runtime_screenshot_images(
            context.conversation[:context.turn_convo_start]
        )
    visible_result = tool_media.model_visible_tool_result(
        tool,
        tool_result,
        image_attached=attach_screenshot,
    )
    if context.native_tool_calls:
        context.conversation.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": _safe_json(visible_result),
        })
        if attach_screenshot:
            context.screenshot_messages.append(screenshot_message)
        return
    follow_up = (
        f"[MCP执行{'失败' if failed else '确认'}]\n"
        f"系统已执行工具：{tool}\n"
        f"执行状态：{'失败' if failed else '成功'}\n\n"
        "[工具参数]\n"
        f"{_safe_json(arguments)}\n\n"
        "[工具执行结果]\n"
        f"{_safe_json(visible_result)}\n\n"
        "请基于以上结果继续完成任务。"
    )
    if attach_screenshot:
        context.conversation.append({
            "role": "user",
            "content": [
                {"type": "text", "text": follow_up},
                *screenshot_message["content"],
            ],
        })
    else:
        context.conversation.append({"role": "user", "content": follow_up})


def append_joined_tool_response(
    conversation: List[Dict[str, Any]],
    original_tool: str,
    items: tuple[Dict[str, object], ...],
    failed: bool,
    call_id: str,
    *,
    native: bool,
) -> None:
    payload = {
        "success": not failed,
        "compat_mode": "split_concatenated_tool_names",
        "original_tool": original_tool,
        "tools": list(items),
    }
    if native:
        conversation.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": _safe_json(payload),
        })
        return
    conversation.append({"role": "user", "content": (
        "[MCP兼容处理完成]\n"
        "系统检测到多个 MCP 工具名被拼接，已按顺序拆分处理。\n"
        "其中安全且参数完整的工具已执行；缺少参数或不适合从拼接调用执行的工具已逐项标记失败。\n\n"
        "[工具处理结果]\n"
        f"{_safe_json(payload)}\n\n"
        "请基于以上结果继续；如仍需调用失败的工具，请按标准格式提供所需参数重新调用。"
    )})


def infer_todo_action(arguments: dict) -> str:
    args = arguments if isinstance(arguments, dict) else {}
    action = str(args.get("action") or "").strip().lower()
    if action:
        return action
    if args.get("phases") is not None or args.get("goal"):
        return "create"
    if any(key in args for key in ("status", "summary", "outcome")):
        return "edit"
    return "get"


def described_tool_entries(
    tool_result: Dict[str, object],
) -> tuple[List[dict], List[str]]:
    payload = tool_result.get("result", tool_result)
    if not isinstance(payload, dict):
        return [], []
    batch = payload.get("tools")
    items = (
        [item for item in batch if isinstance(item, dict)]
        if isinstance(batch, list)
        else [payload]
    )
    names = [str(item.get("name") or "").strip() for item in items]
    return items, names


def append_control_tool_result(
    conversation: List[Dict[str, Any]],
    tool: str,
    visible_result: object,
    call_id: str,
    *,
    native: bool,
) -> None:
    if native:
        conversation.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": _safe_json(visible_result),
        })
        return
    conversation.append({
        "role": "user",
        "content": (
            f"[MCP执行结果]\n系统已执行工具：{tool}\n执行状态：成功\n\n"
            "[工具执行结果]\n"
            f"{_safe_json(visible_result)}"
        ),
    })


def track_repeated_tool_call(
    prefix: str,
    tool: str,
    arguments: dict,
    previous_signature: str,
    previous_count: int,
) -> tuple[str, int]:
    """Return the stable call signature and its consecutive repeat count."""

    signature = (
        f"{prefix}|{tool}|"
        f"{json.dumps(arguments, ensure_ascii=False, sort_keys=True)}"
    )
    count = previous_count + 1 if signature == previous_signature else 1
    return signature, count


def append_pending_call_responses(
    conversation: List[Dict[str, Any]],
    pending: List[Dict[str, Any]],
    payload: Dict[str, object],
    *,
    native: bool,
) -> None:
    """Close tool-call ids skipped by a control-flow transition."""

    if not pending:
        return
    if native:
        conversation.extend(
            {
                "role": "tool",
                "tool_call_id": str(call.get("id") or "call_0"),
                "content": _safe_json(payload),
            }
            for call in pending
        )
        return
    skipped = ", ".join(str(call.get("tool") or "?") for call in pending)
    conversation.append(
        {
            "role": "user",
            "content": (
                "[MCP未执行]\n"
                f"本轮以下工具未被执行：{skipped}\n\n"
                f"{_safe_json(payload)}"
            ),
        }
    )


def flush_screenshot_messages(
    conversation: List[Dict[str, Any]], screenshots: List[Dict[str, Any]]
) -> None:
    """Append held images only after every native tool response is adjacent."""

    conversation.extend(screenshots)
    screenshots.clear()


def to_native_tool_name(name: str) -> str:
    safe = str(name or "").strip().replace(".", "_").replace("+", "-")
    safe = re.sub(r"[^a-zA-Z0-9_-]", "-", safe)
    return (safe.strip("_-") or "tool")[:64]


def build_native_tools_payload(
    allowed_tools: Optional[set] = None,
) -> tuple[List[Dict], Dict[str, str]]:
    tools = []
    native_to_mcp: Dict[str, str] = {}
    used_names = set()
    tool_payloads = registry.build_tools_payload(allowed_tools)
    tool_payloads.extend(build_endpoint_tools_payload(allowed_tools))
    for tool in tool_payloads:
        native_tool = copy.deepcopy(tool)
        original_name = str(native_tool.get("function", {}).get("name") or "").strip()
        native_name = to_native_tool_name(original_name)
        if native_name in used_names and native_to_mcp.get(native_name) != original_name:
            suffix = 2
            base = native_name[:58]
            while f"{base}_{suffix}" in used_names:
                suffix += 1
            native_name = f"{base}_{suffix}"
        native_tool["function"]["name"] = native_name
        native_to_mcp[native_name] = original_name
        used_names.add(native_name)
        tools.append(native_tool)
    return tools, native_to_mcp


def resolve_mcp_tool_name(
    tool: str,
    native_tool_name_map: Dict[str, str],
    allowed_tools: Optional[set] = None,
) -> str:
    name = str(tool or "").strip()
    if not name or name in native_tool_name_map:
        return native_tool_name_map.get(name, "")
    from api.services.mcp.mcp_tool_aliases import resolve_tool_name

    candidates = set(native_tool_name_map.values())
    candidates.update(allowed_tools or set())
    candidates.update(known_mcp_tool_names())
    return resolve_tool_name(name, candidates) or name


def known_mcp_tool_names() -> set[str]:
    """Return current server and online endpoint tool names for error classification."""
    candidates: set[str] = set()
    try:
        candidates.update(
            str(item.get("name") or "").strip()
            for item in registry.list_tools()
            if item.get("name")
        )
    except Exception:
        pass
    try:
        candidates.update(
            str(item.get("name") or "").strip()
            for item in connected_endpoint_tool_catalog()
            if item.get("name")
        )
    except Exception:
        pass
    return candidates


def split_concatenated_native_tool_name(
    name: str, native_tool_name_map: Dict[str, str]
) -> List[str]:
    remaining = str(name or "").strip()
    if not remaining or remaining in native_tool_name_map:
        return []
    native_names = sorted(native_tool_name_map, key=len, reverse=True)
    parts: List[str] = []
    while remaining:
        matched = next((candidate for candidate in native_names if remaining.startswith(candidate)), "")
        if not matched:
            return []
        parts.append(matched)
        remaining = remaining[len(matched):]
    return parts if len(parts) > 1 else []


def missing_required_mcp_args(tool_name: str, arguments: dict) -> List[str]:
    tool = registry.get(tool_name)
    schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
    required = schema.get("required") if isinstance(schema, dict) else []
    if not isinstance(required, list):
        return []
    args = arguments if isinstance(arguments, dict) else {}
    return [
        str(name)
        for name in required
        if str(name) not in args or args.get(str(name)) in (None, "")
    ]


def joined_tool_skip_reason(tool_name: str, arguments: dict, allowed_tools: set) -> str:
    if tool_name not in allowed_tools:
        return f"Tool not allowed for this task: {tool_name}"
    if is_endpoint_agent_tool(tool_name):
        return ""
    tool = registry.get(tool_name)
    missing = missing_required_mcp_args(tool_name, arguments)
    if missing:
        return f"Missing required argument(s) for {tool_name}: {', '.join(missing)}"
    if tool.destructive and not str(tool_name).startswith("prompt."):
        return f"Cannot safely execute destructive tool from a joined MCP call: {tool_name}"
    return ""
