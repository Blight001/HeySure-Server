"""
Provider-aware streaming helpers for OPT-02 (Prompt Caching).

Supports two providers:
  - "openai_compat"  : OpenAI-compatible SSE (DeepSeek, OpenAI, etc.)
  - "anthropic"      : Anthropic Messages API with prompt-caching-2024-07-31 beta

Usage in chat_worker.py:
    provider = _detect_provider(base_url)
    if provider == "anthropic":
        sr = stream_turn_anthropic(run_id, base_url, api_key, model, convo,
                                   step_tools, native_tool_name_map)
    else:
        response = ai_http_post(...)
        _raise_for_upstream_error(response)
        sr = stream_turn_openai_compat(run_id, response, native_tool_name_map)
"""

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

from api.http_client import ai_http_post
from .chat_prompt_utils import (
    _extract_delta_text,
    _extract_first_complete_mcp_call,
    _get_run_live_reasoning,
    _set_run_live_phase,
    _set_run_live_reasoning,
    _set_run_live_text,
    _set_run_live_usage,
)
from .chat_runtime_helpers import _run_should_stop


@dataclass
class StreamResult:
    assistant_text: str = ""
    reasoning_content: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    finish_reason: str = ""
    payload_call: Optional[Dict[str, Any]] = None
    tc_id: str = ""
    tc_name: str = ""
    tc_args: str = ""
    has_native_tc: bool = False
    stopped: bool = False


def _merge_run_reasoning(previous: str, current: str) -> str:
    old = str(previous or "").rstrip()
    new = str(current or "").lstrip()
    if not old:
        return new
    if not new:
        return old
    return f"{old}\n\n{new}"


def _detect_provider(base_url: str) -> str:
    url = str(base_url or "").lower().strip()
    if "api.anthropic.com" in url:
        return "anthropic"
    return "openai_compat"


def _anthropic_endpoint(base_url: str) -> str:
    url = str(base_url or "").strip().rstrip("/")
    if url.endswith("/messages"):
        return url
    if url.endswith("/v1"):
        return f"{url}/messages"
    if "api.anthropic.com" in url:
        return "https://api.anthropic.com/v1/messages"
    return f"{url}/v1/messages"


def _to_anthropic_messages(convo: List[Dict]) -> tuple:
    """Convert OpenAI-format convo to Anthropic (system_blocks, messages).

    Handles:
      - system  → extracted into system_blocks
      - user    → {role: user, content: [{type: text, text: ...}]}
      - assistant with tool_calls → content includes tool_use blocks
      - tool (OpenAI) → merged as tool_result into a user message
    """
    system_parts: List[str] = []
    messages: List[Dict] = []

    def _content_blocks(content: Any) -> List[Dict]:
        if not isinstance(content, list):
            return [{"type": "text", "text": str(content)}]
        blocks: List[Dict] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                blocks.append({"type": "text", "text": str(item.get("text") or "")})
            elif item.get("type") == "image_url":
                url = ((item.get("image_url") or {}).get("url") or "")
                m = re.match(r"^data:([^;,]+);base64,(.+)$", str(url), re.DOTALL)
                if m:
                    blocks.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": m.group(1) or "image/png",
                            "data": m.group(2) or "",
                        },
                    })
        return blocks or [{"type": "text", "text": ""}]

    for msg in convo:
        role = msg.get("role", "")
        content = msg.get("content") or ""

        if role == "system":
            text = str(content).strip()
            if text:
                system_parts.append(text)
            continue

        if role == "user":
            messages.append({
                "role": "user",
                "content": _content_blocks(content),
            })

        elif role == "assistant":
            tool_calls = msg.get("tool_calls") or []
            content_blocks: List[Dict] = []
            if content:
                content_blocks.append({"type": "text", "text": str(content)})
            for tc in tool_calls:
                fn = tc.get("function") or {}
                try:
                    input_dict = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    input_dict = {}
                content_blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id") or "call_0",
                    "name": fn.get("name") or "",
                    "input": input_dict,
                })
            if not content_blocks:
                content_blocks = [{"type": "text", "text": ""}]
            messages.append({"role": "assistant", "content": content_blocks})

        elif role == "tool":
            tool_content = _content_blocks(content)
            tool_result_block = {
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id") or "call_0",
                "content": tool_content if len(tool_content) > 1 or tool_content[0].get("type") != "text" else tool_content[0].get("text", ""),
            }
            # Merge into the last user message if possible; otherwise new user message.
            if messages and messages[-1]["role"] == "user":
                messages[-1]["content"].append(tool_result_block)
            else:
                messages.append({
                    "role": "user",
                    "content": [tool_result_block],
                })

    system_blocks = [{"type": "text", "text": "\n\n".join(system_parts)}]
    return system_blocks, messages


def _apply_anthropic_prefix_cache(
    system_blocks: List[Dict],
    messages: List[Dict],
    min_history: int = 4,
) -> tuple:
    """Add cache_control markers for Anthropic prompt caching.

    Strategy:
      1. Mark the last system block as ephemeral (caches the system prompt).
      2. If there is enough history, mark the message immediately before the
         last user turn as ephemeral (caches the conversation prefix).
    """
    # Deep-copy to avoid mutating the caller's data.
    system_blocks = [dict(b) for b in system_blocks]
    messages = [dict(m) for m in messages]

    if system_blocks:
        system_blocks[-1] = {**system_blocks[-1], "cache_control": {"type": "ephemeral"}}

    if len(messages) >= min_history:
        # Find the index of the last user message (current turn).
        last_user_idx = -1
        for i in range(len(messages) - 1, -1, -1):
            if messages[i]["role"] == "user":
                last_user_idx = i
                break
        # Mark the message just before the current user turn.
        if last_user_idx > 0:
            prev = dict(messages[last_user_idx - 1])
            prev_content = prev.get("content")
            if isinstance(prev_content, list) and prev_content:
                new_content = list(prev_content)
                new_content[-1] = {**new_content[-1], "cache_control": {"type": "ephemeral"}}
                prev["content"] = new_content
            messages[last_user_idx - 1] = prev

    return system_blocks, messages


def stream_turn_openai_compat(
    run_id: str,
    response: requests.Response,
    native_tool_name_map: Dict[str, str],
) -> StreamResult:
    """Stream one turn using the OpenAI-compatible SSE format.

    Processes an already-open streaming response and returns a StreamResult.
    Handles native tool_calls as well as text-based <mcp-call> fallback.
    """
    sr = StreamResult()
    last_push_at = 0.0
    tool_call_parts: Dict[int, Dict[str, str]] = {}
    previous_reasoning = _get_run_live_reasoning(run_id)

    _set_run_live_text(run_id, "")
    _set_run_live_phase(run_id, "generating")
    _set_run_live_usage(run_id, 0, 0, 0)

    for raw_line in response.iter_lines():
        if _run_should_stop(run_id):
            response.close()
            _set_run_live_text(run_id, "")
            sr.stopped = True
            return sr
        if not raw_line:
            continue
        line = raw_line.decode("utf-8")
        if not line.startswith("data: "):
            continue
        payload_line = line[6:].strip()
        if payload_line == "[DONE]":
            break
        try:
            chunk = json.loads(payload_line)
        except Exception:
            continue

        if isinstance(chunk.get("usage"), dict):
            sr.usage = chunk["usage"]
            _set_run_live_usage(
                run_id,
                int(sr.usage.get("prompt_tokens") or 0),
                int(sr.usage.get("completion_tokens") or 0),
                int(sr.usage.get("total_tokens") or 0),
            )

        choices = chunk.get("choices") or []
        if not choices:
            continue

        sr.finish_reason = choices[0].get("finish_reason") or sr.finish_reason
        delta = choices[0].get("delta") or {}

        delta_reasoning = delta.get("reasoning_content")
        if isinstance(delta_reasoning, str):
            sr.reasoning_content += delta_reasoning
            _set_run_live_reasoning(
                run_id,
                _merge_run_reasoning(previous_reasoning, sr.reasoning_content),
            )

        # Native tool_calls (OpenAI / DeepSeek compatible).
        tc_list = delta.get("tool_calls")
        if tc_list:
            sr.has_native_tc = True
            for _tc in tc_list:
                try:
                    tc_index = int(_tc.get("index") or 0)
                except Exception:
                    tc_index = len(tool_call_parts)
                part = tool_call_parts.setdefault(tc_index, {"id": "", "name": "", "arguments": ""})
                if _tc.get("id"):
                    part["id"] = _tc["id"]
                _fn = _tc.get("function") or {}
                if _fn.get("name"):
                    part["name"] += _fn["name"]
                if _fn.get("arguments"):
                    part["arguments"] += _fn["arguments"]
            continue

        delta_text = _extract_delta_text(delta)
        if delta_text:
            if sr.payload_call:
                continue
            sr.assistant_text += delta_text
            # Text-based MCP fallback (only when no native tool call is accumulating).
            if not sr.has_native_tc:
                parsed_call, mcp_match = _extract_first_complete_mcp_call(sr.assistant_text)
                if parsed_call and mcp_match:
                    sr.assistant_text = sr.assistant_text[:mcp_match.end()]
                    sr.payload_call = parsed_call
                    _set_run_live_text(run_id, sr.assistant_text)
                    sr.finish_reason = sr.finish_reason or "mcp_wait"
                    continue
            now = time.time()
            if (now - last_push_at) >= 0.05:
                _set_run_live_text(run_id, sr.assistant_text)
                last_push_at = now

    response.close()
    _set_run_live_text(run_id, sr.assistant_text)

    # Resolve native tool calls into the first sequential payload. The worker
    # intentionally performs one MCP call per turn; if a provider still streams
    # multiple tool calls, keep the first one and let the model continue after
    # the tool result instead of concatenating names/JSON fragments.
    if sr.has_native_tc and tool_call_parts and not sr.tc_name:
        first = next(
            (
                item
                for _, item in sorted(tool_call_parts.items(), key=lambda kv: kv[0])
                if item.get("name")
            ),
            None,
        )
        if first:
            sr.tc_id = first.get("id") or "call_0"
            sr.tc_name = first.get("name") or ""
            sr.tc_args = first.get("arguments") or "{}"

    if sr.has_native_tc and sr.tc_name and not sr.payload_call:
        try:
            tc_arguments = json.loads(sr.tc_args or "{}")
        except Exception:
            tc_arguments = {}
        sr.payload_call = {
            "tool": native_tool_name_map.get(sr.tc_name, sr.tc_name),
            "arguments": tc_arguments,
        }

    return sr


def stream_turn_anthropic(
    run_id: str,
    base_url: str,
    api_key: str,
    model: str,
    convo: List[Dict],
    step_tools: List[Dict],
    native_tool_name_map: Dict[str, str],
) -> StreamResult:
    """Stream one turn via the Anthropic Messages API with prompt caching.

    Builds the request internally (including cache_control markers), streams
    the response, and returns a StreamResult using the same interface as
    stream_turn_openai_compat.
    """
    endpoint = _anthropic_endpoint(base_url)

    system_blocks, messages = _to_anthropic_messages(convo)
    system_blocks, messages = _apply_anthropic_prefix_cache(system_blocks, messages)

    # Convert OpenAI-format tools to Anthropic format.
    anthropic_tools = []
    for tool in (step_tools or []):
        fn = tool.get("function") or {}
        anthropic_tools.append({
            "name": fn.get("name") or "",
            "description": fn.get("description") or "",
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })

    payload: Dict[str, Any] = {
        "model": model,
        "max_tokens": 8192,
        "system": system_blocks,
        "messages": messages,
        "stream": True,
    }
    if anthropic_tools:
        payload["tools"] = anthropic_tools

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "prompt-caching-2024-07-31",
    }

    response = ai_http_post(endpoint, headers=headers, json=payload, timeout=300, stream=True)
    if not response.ok:
        body = str(response.text or "")[:500]
        raise RuntimeError(f"Anthropic API error: HTTP {response.status_code} — {body}")

    sr = StreamResult()
    last_push_at = 0.0
    current_block_type = ""
    current_tool_id = ""
    current_tool_name = ""
    current_tool_args = ""
    anthropic_tool_calls: List[Dict[str, str]] = []
    previous_reasoning = _get_run_live_reasoning(run_id)

    _set_run_live_text(run_id, "")
    _set_run_live_phase(run_id, "generating")
    _set_run_live_usage(run_id, 0, 0, 0)

    for raw_line in response.iter_lines():
        if _run_should_stop(run_id):
            response.close()
            _set_run_live_text(run_id, "")
            sr.stopped = True
            return sr
        if not raw_line:
            continue
        line = raw_line.decode("utf-8")
        # Skip "event: ..." lines; only parse "data: ..." lines.
        if not line.startswith("data: "):
            continue
        payload_line = line[6:].strip()
        if not payload_line:
            continue
        try:
            event = json.loads(payload_line)
        except Exception:
            continue

        etype = event.get("type") or ""

        if etype == "message_start":
            msg_data = event.get("message") or {}
            usage_data = msg_data.get("usage") or {}
            sr.usage["prompt_tokens"] = int(usage_data.get("input_tokens") or 0)
            sr.usage["cache_read_input_tokens"] = int(usage_data.get("cache_read_input_tokens") or 0)
            sr.usage["cache_creation_input_tokens"] = int(usage_data.get("cache_creation_input_tokens") or 0)

        elif etype == "content_block_start":
            block = event.get("content_block") or {}
            current_block_type = block.get("type") or ""
            if current_block_type == "tool_use":
                sr.has_native_tc = True
                current_tool_id = block.get("id") or ""
                current_tool_name = block.get("name") or ""
                current_tool_args = ""

        elif etype == "content_block_delta":
            delta = event.get("delta") or {}
            dtype = delta.get("type") or ""
            if dtype == "text_delta":
                text = delta.get("text") or ""
                if text and not sr.payload_call:
                    sr.assistant_text += text
                    now = time.time()
                    if (now - last_push_at) >= 0.05:
                        _set_run_live_text(run_id, sr.assistant_text)
                        last_push_at = now
            elif dtype == "input_json_delta":
                current_tool_args += delta.get("partial_json") or ""
            elif dtype == "thinking_delta":
                thinking = delta.get("thinking") or delta.get("text") or ""
                if thinking:
                    sr.reasoning_content += thinking
                    _set_run_live_reasoning(
                        run_id,
                        _merge_run_reasoning(previous_reasoning, sr.reasoning_content),
                    )

        elif etype == "content_block_stop":
            if current_block_type == "tool_use" and current_tool_name:
                anthropic_tool_calls.append({
                    "id": current_tool_id,
                    "name": current_tool_name,
                    "arguments": current_tool_args,
                })
            current_block_type = ""

        elif etype == "message_delta":
            delta = event.get("delta") or {}
            sr.finish_reason = delta.get("stop_reason") or sr.finish_reason
            usage_out = event.get("usage") or {}
            sr.usage["completion_tokens"] = int(usage_out.get("output_tokens") or 0)
            sr.usage["total_tokens"] = (
                (sr.usage.get("prompt_tokens") or 0) + (sr.usage.get("completion_tokens") or 0)
            )
            _set_run_live_usage(
                run_id,
                int(sr.usage.get("prompt_tokens") or 0),
                int(sr.usage.get("completion_tokens") or 0),
                int(sr.usage.get("total_tokens") or 0),
            )

        elif etype == "message_stop":
            break

    response.close()
    _set_run_live_text(run_id, sr.assistant_text)

    # Resolve the first tool call. The chat worker executes MCP sequentially.
    if sr.has_native_tc and anthropic_tool_calls and not sr.tc_name:
        first = anthropic_tool_calls[0]
        sr.tc_id = first.get("id") or "call_0"
        sr.tc_name = first.get("name") or ""
        sr.tc_args = first.get("arguments") or "{}"

    if sr.has_native_tc and sr.tc_name:
        try:
            tc_arguments = json.loads(sr.tc_args or "{}")
        except Exception:
            tc_arguments = {}
        sr.payload_call = {
            "tool": native_tool_name_map.get(sr.tc_name, sr.tc_name),
            "arguments": tc_arguments,
        }

    return sr
