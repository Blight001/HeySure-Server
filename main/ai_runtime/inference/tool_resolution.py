"""Native provider tool-name encoding, aliases, and safe joined-call checks."""

import copy
import json
import re
from enum import Enum
from typing import Any, Dict, List, Optional

from api.chat_runtime.chat_prompt_utils import _safe_json
from connector_runtime.dispatch.desktop_device_tools import (
    build_endpoint_tools_payload,
    is_endpoint_agent_tool,
)
from mcp_runtime.mcp import registry


class TurnCallAction(str, Enum):
    """State transition requested after one tool call in a model turn."""

    NEXT_CALL = "next_call"
    NEXT_TURN = "next_turn"
    STOP_RUN = "stop_run"


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
    try:
        candidates.update(
            str(item.get("name") or "").strip()
            for item in registry.list_tools()
            if item.get("name")
        )
    except Exception:
        pass
    return resolve_tool_name(name, candidates) or name


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
