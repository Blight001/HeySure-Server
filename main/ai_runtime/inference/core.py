IS_ROUTER_ENTRY = False

import asyncio
import base64
import copy
import json
import logging
import os
import re
import sys
import time


logger = logging.getLogger(__name__)
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests
from sqlmodel import Session, select

from api.database import engine
from api.runtime.async_bridge import run_async
from api.runtime.http_client import ai_http_post
from mcp_runtime.mcp import get_project_root, registry
from mcp_runtime.mcp.core import MCP_INTROSPECTION_TOOLS
from api.models import AITaskJob, AssistantAIConfig, ChatMessage, ChatMessageCreate, User
from api.services.chat import conversation_compress
from api.services.tasks import task_plan as plan_service
from ai_runtime.inference import ai_message_service
from ai_runtime.inference import phase_context
from connector_runtime.dispatch.device_dispatch import (
    dispatch_endpoint_tool_and_wait,
    get_run_session_context,
    set_run_session_context,
)
from api.services.tasks.task_completion_notify import notify_task_completion
from connector_runtime.dispatch.desktop_device_tools import (
    build_endpoint_tools_payload,
    endpoint_bridge_tools_for_config,
    endpoint_tools_for_config,
    is_endpoint_agent_tool,
    is_workshop_tool,
)
from api.services.tasks.task_system import (
    TASK_PLAN_FLOW_PROMPT,
    TASK_RUNTIME_REQUIRED_TOOLS,
    normalize_system_auto_control,
    with_workspace_read_by_name_compat,
)
from api.chat_runtime.chat_prompt_utils import (
    _append_mcp_state_to_tags,
    _append_prompt_section,
    _build_mcp_display_result,
    _build_mcp_stream_warning,
    _extract_first_mcp_call,
    _extract_mcp_error,
    _filter_tools_for_current_bindings,
    _sanitize_large_media,
    _safe_json,
    _set_run_live_meta,
    _set_run_live_phase,
    _set_run_live_text,
    _set_run_live_usage,
    _strip_prompt_section,
    _strip_task_runtime_sections,
)
from api.services.chat.chat_persistence import _save_message
from api.chat_runtime.chat_stream import _detect_provider, stream_turn_anthropic, stream_turn_openai_compat
from api.chat_runtime.chat_runtime_helpers import (
    _create_loop_scheduled_job,
    _is_task_finished_status,
    _load_task_job_by_session,
    _load_task_payload_by_session,
    _parse_allowed_tools,
    _resolve_ai_runtime,
    _run_set_status,
    _run_should_stop,
    _session_total_tokens,
    build_runtime_system_prompt_and_tools,
)

from api.core.config import DEFAULT_CHAT_MAX_STEPS
from api.core.settings import settings


def _ai_debug_enabled() -> bool:
    return bool(settings.ai_debug)


def _ai_debug_color_enabled() -> bool:
    # ai_debug_color defaults True, so honor NO_COLOR (standard convention)
    # and TTY autodetect on top of the explicit setting.
    if not settings.ai_debug_color:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def _ai_color(text: str, code: str) -> str:
    if not _ai_debug_color_enabled():
        return text
    return f"\033[{code}m{text}\033[0m"


def _ai_short(value: Any, limit: int = 48) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    if len(text) <= limit:
        return text
    return f"{text[: max(1, limit - 1)]}…"


def _ai_short_run_id(run_id: str) -> str:
    text = str(run_id or "").strip()
    if not text:
        return "-"
    if text.startswith("run_") and len(text) > 12:
        return f"run_{text[4:12]}"
    return _ai_short(text, 16)


def _ai_short_base_url(base_url: str) -> str:
    text = str(base_url or "").strip()
    if not text:
        return "-"
    parsed = urlparse(text)
    if not parsed.netloc:
        return _ai_short(text, 48)
    path = parsed.path.rstrip("/")
    if path:
        return f"{parsed.netloc}{path}"
    return parsed.netloc


def _ai_debug_log(message: str) -> None:
    if _ai_debug_enabled():
        logger.debug(message)


def _ai_debug_stage(stage: str, message: str, color: str = "36") -> None:
    _ai_debug_log(f"{_ai_color(stage, color)} {message}")


def _normalize_ai_message_type(value: Any, require_reply: bool) -> str:
    text = str(value or "").strip().lower()
    if text in {"inquiry", "reply", "chitchat", "notify"}:
        return text
    return "inquiry" if require_reply else "notify"


def _render_ai_message_system_prompt(
    *,
    from_ai_name: str,
    from_ai_config_id: int,
    target_ai_name: str,
    target_ai_config_id: int,
    message_id: str,
    current_session_id: str,
    content: str,
    message_type: str,
    require_reply: bool,
) -> str:
    should_reply = bool(require_reply) or message_type == "inquiry"
    message_type_guide = (
        "- inquiry（询问）：发送方在提问、请求状态或请求结果，通常需要你答复。\n"
        "- reply（回复）：发送方在答复你之前发出的 inquiry，通常不需要再答复，除非内容明确提出新问题。\n"
        "- notify（通知）：发送方在单向告知状态、结果或提醒，不期待你回复。\n"
        "- chitchat（闲聊）：非任务型闲聊，可自然继续多轮。"
    )
    reply_rule = (
        "这条消息需要你回复。回复时调用 MCP 工具 `message.send_to_ai`，"
        f"参数必须包含 `to_ai_config_id={from_ai_config_id}`、`message_type=\"reply\"`、"
        "`require_reply=false`、"
        f"`reply_to_message_id=\"{message_id}\"`、`current_session_id=\"{current_session_id}\"`。"
        if should_reply
        else "这条消息不要求回复。除非内容明确要求你另起一个新问题，否则不要回信。"
    )
    return (
        "[系统提示]\n"
        "[AI 间通信 · 强制插入]\n"
        "当前 AI 运行已被这条消息打断。你必须先处理这条系统提示，再继续原本任务。\n\n"
        f"- 收件方（你）: {target_ai_name}（ai_config_id={target_ai_config_id}）\n"
        f"- 发送方: {from_ai_name}（ai_config_id={from_ai_config_id}）\n"
        f"- 消息编号: {message_id}\n"
        f"- 当前会话: {current_session_id}\n"
        f"- 消息类型: {message_type}\n"
        f"- 是否要求回复: {'是' if should_reply else '否'}\n\n"
        "[消息内容]\n"
        f"{content}\n\n"
        "[发送类型说明]\n"
        f"{message_type_guide}\n\n"
        "[处理规则]\n"
        "你以后调用 MCP 工具 `message.send_to_ai` 时，`message_type` 是必填字段，不能省略。\n"
        f"{reply_rule}"
    )


def _coerce_max_steps(value: object, default: int = 48) -> int:
    try:
        return max(1, min(999, int(value or default)))
    except Exception:
        return max(1, min(999, int(default)))


def _resolve_ai_name_safe(session: Session, ai_config_id: Optional[int]) -> str:
    if not ai_config_id:
        return ""
    try:
        row = session.exec(
            select(AssistantAIConfig).where(AssistantAIConfig.id == int(ai_config_id))
        ).first()
        return str(row.name or "") if row else ""
    except Exception:
        return ""


def _format_upstream_error(response: requests.Response, max_body_len: int = 4000) -> str:
    status = f"HTTP {response.status_code}"
    reason = str(response.reason or "").strip()
    if reason:
        status = f"{status} {reason}"

    body = str(response.text or "").strip()
    if body:
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                error = parsed.get("error")
                if isinstance(error, dict):
                    message = str(error.get("message") or "").strip()
                    code = str(error.get("code") or "").strip()
                    error_type = str(error.get("type") or "").strip()
                    parts = [part for part in [message, code, error_type] if part]
                    if parts:
                        body = " | ".join(parts)
                elif isinstance(error, str) and error.strip():
                    body = error.strip()
        except Exception:
            pass
    if len(body) > max_body_len:
        body = f"{body[:max_body_len]}\n...<truncated>"
    return f"Upstream AI request failed: {status} for {response.url}\n{body}".strip()


def _raise_for_upstream_error(response: requests.Response) -> None:
    if response.ok:
        return
    raise RuntimeError(_format_upstream_error(response))


def _to_native_tool_name(name: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "__", str(name or "").strip())
    safe = safe.strip("_") or "tool"
    return safe[:64]


def _build_native_tools_payload(allowed_tools: Optional[set] = None) -> tuple[List[Dict], Dict[str, str]]:
    tools = []
    native_to_mcp: Dict[str, str] = {}
    used_names = set()
    tool_payloads = registry.build_tools_payload(allowed_tools)
    tool_payloads.extend(build_endpoint_tools_payload(allowed_tools))
    for tool in tool_payloads:
        native_tool = copy.deepcopy(tool)
        original_name = str(native_tool.get("function", {}).get("name") or "").strip()
        native_name = _to_native_tool_name(original_name)
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


def _split_concatenated_native_tool_name(name: str, native_tool_name_map: Dict[str, str]) -> List[str]:
    """Return native tool names when a model accidentally joins multiple names."""
    remaining = str(name or "").strip()
    if not remaining or remaining in native_tool_name_map:
        return []
    native_names = sorted(native_tool_name_map.keys(), key=len, reverse=True)
    parts: List[str] = []
    while remaining:
        matched = next((candidate for candidate in native_names if remaining.startswith(candidate)), "")
        if not matched:
            return []
        parts.append(matched)
        remaining = remaining[len(matched):]
    return parts if len(parts) > 1 else []


def _missing_required_mcp_args(tool_name: str, arguments: dict) -> List[str]:
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


def _joined_tool_skip_reason(tool_name: str, arguments: dict, allowed_tools: set) -> str:
    if tool_name not in allowed_tools:
        return f"Tool not allowed for this task: {tool_name}"
    if is_endpoint_agent_tool(tool_name):
        return ""
    tool = registry.get(tool_name)
    missing = _missing_required_mcp_args(tool_name, arguments)
    if missing:
        return f"Missing required argument(s) for {tool_name}: {', '.join(missing)}"
    if tool.destructive and not str(tool_name).startswith("prompt."):
        return f"Cannot safely execute destructive tool from a joined MCP call: {tool_name}"
    return ""


async def _call_mcp_via_runtime(
    runtime_url: str,
    tool: str,
    user_id: int,
    arguments: dict,
    ai_config_id: Optional[int],
) -> Dict[str, object]:
    """Forward an MCP tool call to ``mcp-runtime`` over HTTP.

    Lazy-imports httpx so the in-process path keeps zero overhead. Uses the
    INTERNAL_TOKEN Bearer header from ``runtime.internal_http.internal_headers``.
    """
    from api.runtime.internal_http import internal_post

    # Carry the current run's session context across the process boundary so
    # tools in mcp-runtime can resolve the active session_id / channel /
    # identity even though they run in a separate process where the worker's
    # contextvar is not set. None in the in-process path; harmless to send.
    body = {
        "tool": tool,
        "user_id": user_id,
        "ai_config_id": ai_config_id,
        "arguments": arguments,
    }
    run_ctx = get_run_session_context()
    if run_ctx:
        body["session_context"] = run_ctx

    return await internal_post(runtime_url, "/internal/mcp/call", json=body, timeout=120.0)


async def _dispatch_endpoint_via_runtime(
    runtime_url: str,
    tool: str,
    user_id: int,
    arguments: dict,
    ai_config_id: Optional[int],
    timeout_seconds: int = 120,
    poll_interval: float = 0.25,
) -> Dict[str, object]:
    """Forward an endpoint-agent tool dispatch to ``connector-runtime`` and
    poll the persisted task row until it finishes.

    Polling (vs blocking HTTP) is what makes the dispatch survive a
    connector-runtime restart: once the row is in the DB, any subsequent
    poll picks up the agent's eventual reply even if the original wait
    process was wiped.
    """
    import asyncio as _asyncio
    import httpx
    from api.runtime.internal_http import internal_headers

    headers = internal_headers()
    base = runtime_url.rstrip("/")

    async with httpx.AsyncClient(base_url=base, timeout=30.0) as client:
        post_resp = await client.post(
            "/internal/agent/dispatch",
            headers=headers,
            json={
                "user_id": user_id,
                "ai_config_id": ai_config_id,
                "tool": tool,
                "arguments": arguments,
            },
        )
        post_resp.raise_for_status()
        post_body = post_resp.json()
        task_id = str(post_body.get("task_id") or "")
        if not task_id:
            return {
                "success": False,
                "tool": tool,
                "error": "connector-runtime returned no task_id",
            }

        deadline = _asyncio.get_running_loop().time() + max(1, int(timeout_seconds))
        consecutive_missing = 0
        while True:
            row: Dict[str, Any]
            try:
                resp = await client.get(
                    f"/internal/agent/dispatch/result/{task_id}",
                    headers=headers,
                )
                if resp.status_code == 404:
                    # Row should exist (we just POSTed) — a 404 means the
                    # connector-runtime restart wiped our row between the
                    # POST and our first GET, or some other race we should
                    # not paper over indefinitely.
                    consecutive_missing += 1
                    if consecutive_missing >= 3:
                        return {
                            "success": False,
                            "taskId": task_id,
                            "tool": tool,
                            "error": "dispatch row missing after retries (connector-runtime restart?)",
                        }
                    row = {"status": "pending"}
                else:
                    resp.raise_for_status()
                    row = resp.json()
                    consecutive_missing = 0
            except Exception as exc:
                # Transient HTTP failure: keep polling until the deadline.
                row = {"status": "pending", "error": f"poll error: {exc}"}
            status = str(row.get("status") or "pending")
            if status not in {"pending", "queued"}:
                return {
                    "success": bool(row.get("success", status == "completed")),
                    "taskId": task_id,
                    "tool": row.get("tool") or tool,
                    "summary": row.get("summary"),
                    "result": row.get("result"),
                    "error": row.get("error"),
                }
            if _asyncio.get_running_loop().time() >= deadline:
                # Tell the socket-owning process to finalize the row and
                # resume the device queue — a row left "pending" blocks every
                # later dispatch to that device until the orphan sweep.
                try:
                    await client.post(
                        f"/internal/agent/dispatch/expire/{task_id}",
                        headers=headers,
                        json={"reason": f"Endpoint agent result timeout after {timeout_seconds}s"},
                    )
                except Exception:
                    pass  # best-effort; the periodic orphan sweep is the backstop
                return {
                    "success": False,
                    "taskId": task_id,
                    "tool": tool,
                    "error": f"Endpoint agent result timeout after {timeout_seconds}s",
                }
            await _asyncio.sleep(poll_interval)


async def _call_mcp_or_endpoint_tool(
    tool: str,
    user_id: int,
    arguments: dict,
    ai_config_id: Optional[int],
) -> Dict[str, object]:
    if is_workshop_tool(tool):
        return {
            "tool": tool,
            "destructive": True,
            "result": await dispatch_endpoint_tool_and_wait(
                user_id=user_id,
                ai_config_id=ai_config_id,
                tool=tool,
                args=arguments,
            ),
        }
    if is_endpoint_agent_tool(tool):
        # Desktop / browser agents register their socket on the api-gateway, so
        # endpoint-tool dispatch is served there (gateway.routers.
        # device_dispatch_internal). In the monolith api_gateway_url is empty and
        # we dispatch in-process below.
        agent_host_url = settings.api_gateway_url
        if agent_host_url:
            return {
                "tool": tool,
                "destructive": True,
                "result": await _dispatch_endpoint_via_runtime(
                    agent_host_url, tool, user_id, arguments, ai_config_id
                ),
            }
        return {
            "tool": tool,
            "destructive": True,
            "result": await dispatch_endpoint_tool_and_wait(
                user_id=user_id,
                ai_config_id=ai_config_id,
                tool=tool,
                args=arguments,
            ),
        }
    # Web search is a direct outbound API call. Keep it in-process so an
    # unavailable split MCP runtime cannot turn a healthy search API into a
    # 127.0.0.1:3001 proxy failure.
    if tool == "workspace.search":
        return await registry.call(tool, user_id, arguments, ai_config_id)

    runtime_url = settings.mcp_runtime_url
    if runtime_url:
        return await _call_mcp_via_runtime(runtime_url, tool, user_id, arguments, ai_config_id)
    return await registry.call(tool, user_id, arguments, ai_config_id)


def _record_mcp_call(
    *, tool, user_id, ai_config_id, session_id, run_id, message_id, failed, error
) -> None:
    """Best-effort record of a tool call outcome for the failure-rate view.

    Captures the conversation location (session / run / message) so a failure can
    be traced in chat and the responsible tool iterated. Never breaks the run."""
    try:
        from api.services.mcp import mcp_stats

        mcp_stats.record_call(
            user_id=user_id,
            ai_config_id=ai_config_id,
            tool=tool,
            success=not failed,
            error=error or "",
            session_id=str(session_id or ""),
            run_id=str(run_id or ""),
            message_id=getattr(message_id, "id", message_id) if message_id is not None else None,
        )
    except Exception:
        pass


def _tool_result_failed(tool_result: Dict[str, object]) -> tuple[bool, str]:
    result = tool_result.get("result") if isinstance(tool_result, dict) else None
    if isinstance(result, dict) and result.get("success") is False:
        detail = (
            result.get("error")
            or result.get("summary")
            or result.get("stderr")
            or result.get("output")
            or result.get("failure_type")
            or "Tool returned success=false"
        )
        if result.get("failure_type") and result.get("exit_code") is not None:
            detail = f"{result.get('failure_type')} (exit_code={result.get('exit_code')}): {detail}"
        elif result.get("failure_type"):
            detail = f"{result.get('failure_type')}: {detail}"
        return True, str(detail)
    return False, ""


def _append_missing_tool_responses(convo: List[Dict], error_text: str) -> List[str]:
    """Repair OpenAI-style history in-place.

    OpenAI-compatible providers require every assistant message with tool_calls
    to be followed immediately by tool messages for each tool_call_id. Appending
    synthetic tool responses to the end is still invalid if a user/system message
    already sits between the assistant tool_calls and the tool response, so the
    repair inserts missing tool messages at the exact required position.
    """
    repaired_ids: List[str] = []
    idx = 0
    while idx < len(convo):
        item = convo[idx]
        if item.get("role") != "assistant" or not item.get("tool_calls"):
            if item.get("role") == "tool":
                # Orphan tool messages are invalid in OpenAI-compatible payloads.
                # They can appear after an older failed repair appended a tool
                # response behind a user notice. Drop them from the outgoing
                # in-memory request; persisted user/assistant history is untouched.
                convo.pop(idx)
                continue
            idx += 1
            continue

        tool_calls = item.get("tool_calls") or []
        expected_ids = [
            str(call.get("id") or "").strip()
            for call in tool_calls
            if isinstance(call, dict) and str(call.get("id") or "").strip()
        ]
        if not expected_ids:
            idx += 1
            continue

        seen_ids = set()
        insert_at = idx + 1
        while insert_at < len(convo) and convo[insert_at].get("role") == "tool":
            tool_call_id = str(convo[insert_at].get("tool_call_id") or "").strip()
            if tool_call_id in expected_ids and tool_call_id not in seen_ids:
                seen_ids.add(tool_call_id)
                insert_at += 1
                continue
            convo.pop(insert_at)

        missing_ids = [tool_call_id for tool_call_id in expected_ids if tool_call_id not in seen_ids]
        for offset, tool_call_id in enumerate(missing_ids):
            convo.insert(insert_at + offset, {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": _safe_json({
                    "success": False,
                    "error": error_text,
                    "recovered": True,
                }),
            })
        repaired_ids.extend(missing_ids)
        idx = insert_at + len(missing_ids)
    return repaired_ids


_SCREENSHOT_BUBBLE_MARKER = "[截图]"


def _build_mcp_tool_bubble_content(
    tool: str,
    arguments: dict,
    result_text: str,
    failed: bool = False,
    image_url: str = "",
) -> str:
    status = "失败" if failed else "成功"
    content = (
        "[MCP工具]\n"
        f"工具: {tool}\n"
        f"状态: {status}\n\n"
        "[参数]\n"
        f"{_safe_json(arguments or {})}\n\n"
        "[结果]\n"
        f"{result_text}"
    )
    if image_url:
        # Trailing marker the web UI parses out to render the screenshot inline.
        content += f"\n\n{_SCREENSHOT_BUBBLE_MARKER}\n{image_url}"
    return content


def _save_mcp_tool_bubble(
    bg: Session,
    *,
    user_id: int,
    ai_config_id: Optional[int],
    ai_kind: str,
    session_id: str,
    session_name: str,
    model: str,
    tool: str,
    arguments: dict,
    result_text: str,
    failed: bool = False,
    image_url: str = "",
    image_data_url: str = "",
    tool_result: Optional[Dict[str, object]] = None,
) -> None:
    bot_delivery: Dict[str, object] = {}
    saved = _save_message(
        bg,
        user_id,
        ChatMessageCreate(
            role="system",
            content=_build_mcp_tool_bubble_content(tool, arguments, result_text, failed, image_url),
            tags="mcp_tool_call",
            ai_config_id=ai_config_id,
            ai_kind=ai_kind,
            session_id=session_id,
            session_name=session_name,
            model=model,
            total_tokens=0,
        ),
    )
    if image_data_url and saved.id:
        try:
            from api.services.chat.chat_media import message_media_url, save_message_image_data_url

            media = save_message_image_data_url(bg, message=saved, data_url=image_data_url)
            saved.content = _build_mcp_tool_bubble_content(
                tool,
                arguments,
                result_text,
                failed,
                message_media_url(media),
            )
            bg.add(saved)
            bg.commit()
        except Exception:
            logger.debug("screenshot chat-media persist skipped", exc_info=True)
    if (not failed) and tool_result and _screenshot_send_to_user_enabled(tool, tool_result, arguments):
        try:
            bot_delivery = _deliver_screenshot_to_bot(bg, saved, tool_result=tool_result)
            if bot_delivery:
                saved.content = _build_mcp_tool_bubble_content(
                    tool,
                    arguments,
                    f"{result_text}\n\n[机器人发送]\n{_safe_json(bot_delivery)}",
                    failed,
                    _extract_screenshot_bubble_url(saved.content) or image_url,
                )
                bg.add(saved)
                bg.commit()
        except Exception:
            logger.debug("screenshot bot delivery skipped", exc_info=True)


def _extract_screenshot_bubble_url(content: str) -> str:
    marker = f"\n\n{_SCREENSHOT_BUBBLE_MARKER}\n"
    text = str(content or "")
    if marker not in text:
        return ""
    return text.rsplit(marker, 1)[-1].strip().splitlines()[0].strip()


def _load_current_user_content(
    session: Session,
    *,
    user_id: int,
    ai_config_id: Optional[int],
    ai_kind: str,
    session_id: str,
    current_user_message_id: Optional[int],
    fallback: Optional[str],
) -> str:
    fallback_text = str(fallback or "").strip()
    if current_user_message_id:
        row = session.get(ChatMessage, current_user_message_id)
        if (
            row
            and row.user_id == user_id
            and row.ai_config_id == ai_config_id
            and row.ai_kind == ai_kind
            and row.session_id == session_id
            and row.role == "user"
        ):
            return fallback_text or str(row.content or "").strip()
    return fallback_text


def _reset_convo_after_clear(
    convo: List[Dict],
    *,
    system_prompt: str,
    current_user_content: str,
    tool_result: Dict[str, object],
) -> None:
    result_payload = tool_result.get("result", tool_result) if isinstance(tool_result, dict) else tool_result
    follow_up = (
        "[MCP执行确认]\n"
        "系统已执行工具：conversation.manage（action=clear）\n"
        "执行状态：成功\n\n"
        "[工具执行结果]\n"
        f"{_safe_json(result_payload)}\n\n"
        "旧上下文已从本轮模型上下文中移除。请只基于当前用户消息和以上结果继续。"
    )
    convo.clear()
    convo.append({"role": "system", "content": system_prompt})
    if current_user_content:
        convo.append({"role": "user", "content": current_user_content})
    convo.append({"role": "user", "content": follow_up})


_IMAGE_DATA_URL_KEYS = ("dataUrl", "data_url", "imageDataUrl", "screenshotDataUrl", "screenshot")
_IMAGE_URL_KEYS = ("image_url", "public_url")
_IMAGE_PATH_KEYS = ("server_path", "path")
_SCREENSHOT_MODEL_TOOLS = {
    "browser_screenshot",
    "mouse.click",
    "screen.capture",
    "screen.capture_region",
    "vision.capture",
    "vision.capture_mouse",
}


def _is_image_input_unsupported_error(error_text: str) -> bool:
    text = str(error_text or "").lower()
    image_markers = (
        "image_url",
        "image input",
        "image content",
        "images are",
        "multimodal",
        "vision input",
    )
    incompatibility_markers = (
        "unknown variant",
        "expected text",
        "not support",
        "unsupported",
        "only supports text",
        "text-only",
        "invalid content type",
        "failed to deserialize",
    )
    return any(marker in text for marker in image_markers) and any(
        marker in text for marker in incompatibility_markers
    )


def _degrade_image_messages_to_text(convo: List[Dict]) -> int:
    removed = 0
    for message in convo:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        kept = []
        message_removed = 0
        for block in content:
            block_type = str(block.get("type") or "").lower() if isinstance(block, dict) else ""
            if block_type in {"image", "image_url"}:
                removed += 1
                message_removed += 1
                continue
            kept.append(block)
        if message_removed:
            kept.append({
                "type": "text",
                "text": f"[系统已省略 {message_removed} 张图片：当前模型不支持图片输入。]",
            })
            message["content"] = kept
    return removed


def _prune_prior_runtime_screenshot_images(convo: List[Dict]) -> int:
    """Keep only the latest runtime screenshot image in model context.

    Chat bubbles still persist each screenshot for the user. This only removes
    older screenshot image blocks from the live model conversation so repeated
    screen/browser captures do not keep charging vision input over and over.
    """
    removed = 0
    screenshot_markers = (
        "工具截图已捕获",
        "鼠标点击前确认图已捕获",
    )
    for message in convo:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        text = "\n".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and str(block.get("type") or "").lower() == "text"
        )
        if not any(marker in text for marker in screenshot_markers):
            continue
        kept = []
        message_removed = 0
        for block in content:
            block_type = str(block.get("type") or "").lower() if isinstance(block, dict) else ""
            if block_type in {"image", "image_url"}:
                removed += 1
                message_removed += 1
                continue
            kept.append(block)
        if message_removed:
            kept.append({
                "type": "text",
                "text": f"[系统已省略 {message_removed} 张较早截图：模型上下文只保留最新截图。]",
            })
            message["content"] = kept
    return removed


def _image_input_degraded_feedback(error_text: str, removed_images: int) -> str:
    return "\n".join([
        "[运行时图片输入错误]",
        f"上游模型拒绝了图片输入，系统已从模型上下文中移除 {removed_images} 张图片。",
        "你无法查看这些图片。请基于已有文字、工具返回的非图片信息继续执行；",
        "如果任务必须依赖视觉内容，请明确说明该限制或改用不需要视觉输入的工具，不要假装已经看过图片。",
        "",
        "[上游错误]",
        str(error_text or "").strip(),
    ])


def _find_image_payload(value: object, depth: int = 0) -> Dict[str, str]:
    if depth > 5:
        return {}
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("data:image/"):
            return {"data_url": text}
        return {}
    if isinstance(value, dict):
        for key in _IMAGE_DATA_URL_KEYS:
            item = value.get(key)
            if isinstance(item, str) and item.strip().startswith("data:image/"):
                return {"data_url": item.strip()}
        for key in _IMAGE_URL_KEYS:
            item = value.get(key)
            if isinstance(item, str) and item.strip().startswith(("http://", "https://")):
                return {"url": item.strip()}
        for key in _IMAGE_PATH_KEYS:
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return {"path": item.strip()}
        for key in ("result", "payload", "data", "screenshot_result"):
            found = _find_image_payload(value.get(key), depth + 1)
            if found:
                return found
        for item in value.values():
            if isinstance(item, (dict, list)):
                found = _find_image_payload(item, depth + 1)
                if found:
                    return found
    if isinstance(value, list):
        for item in value:
            found = _find_image_payload(item, depth + 1)
            if found:
                return found
    return {}


def _image_path_to_data_url(path: str) -> str:
    server_path = str(path or "").strip()
    if not server_path or not os.path.isfile(server_path):
        return ""
    ext = os.path.splitext(server_path)[1].lower().lstrip(".")
    media_type = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
    }.get(ext, "image/png")
    try:
        with open(server_path, "rb") as fh:
            return f"data:{media_type};base64,{base64.b64encode(fh.read()).decode('ascii')}"
    except Exception:
        return ""


def _omit_image_fields(value: object) -> object:
    omitted = {*_IMAGE_DATA_URL_KEYS, "server_path", "workspace_path"}
    if isinstance(value, dict):
        return {
            key: _omit_image_fields(item)
            for key, item in value.items()
            if key not in omitted
        }
    if isinstance(value, list):
        return [_omit_image_fields(item) for item in value]
    return value


def _tool_image_message(tool: str, tool_result: Dict[str, object]) -> Optional[Dict[str, object]]:
    if tool not in _SCREENSHOT_MODEL_TOOLS or not isinstance(tool_result, dict):
        return None
    result_payload = tool_result.get("result", tool_result)
    image_payload = _find_image_payload(tool_result)
    public_image_url = image_payload.get("url", "")
    data_url = image_payload.get("data_url", "")
    if not data_url.startswith("data:image/"):
        data_url = _image_path_to_data_url(image_payload.get("path", ""))
    if not data_url.startswith("data:image/"):
        if not public_image_url.startswith(("http://", "https://")):
            return None
    meta_payload = result_payload if isinstance(result_payload, dict) else {}
    if tool == "mouse.click":
        confirmation = meta_payload.get("confirmation") if isinstance(meta_payload, dict) else {}
        if not isinstance(confirmation, dict):
            confirmation = {}
        target = confirmation.get("target") if isinstance(confirmation.get("target"), dict) else {}
        detail = "\n".join(
            part for part in [
                "鼠标点击前确认图已捕获。注意：本次 mouse.click 尚未执行点击，只返回了红点/十字标注的目标点局部截图。请直接检查红点是否落在目标可点击中心。",
                f"Target: x={target.get('x')}, y={target.get('y')}".strip(),
                "如果红点偏离目标，请估算目标中心相对红点的像素偏差 correction_dx/correction_dy，并再次调用 mouse.click 使用修正后的 x/y 获取新的确认图；如果红点正确，必须再次调用 mouse.click 并传 confirmed:true 才真正点击。",
            ]
            if part and "None" not in part
        )
    else:
        detail = "\n".join(
            part for part in [
                "工具截图已捕获。你已经收到这张图片，请直接查看视觉内容并继续，不要让用户打开本地路径。",
                f"URL: {meta_payload.get('url') or ''}".strip(),
                f"Method: {meta_payload.get('method') or ''}".strip(),
            ]
            if part and not part.endswith(":")
        )
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": detail},
            {"type": "image_url", "image_url": {"url": public_image_url or data_url}},
        ],
    }


def _browser_screenshot_image_message(tool: str, tool_result: Dict[str, object]) -> Optional[Dict[str, object]]:
    return _tool_image_message(tool, tool_result)


def _screenshot_display_ref(tool: str, tool_result: Dict[str, object]) -> Dict[str, str]:
    """Return image data for screenshot chat rendering.

    Screenshot MCP tools hand back the captured image as a public URL, a
    base64 data URL, or a server path. Data URLs/server paths are persisted
    against the eventual ChatMessage row, so deleting or recalling that message
    removes the screenshot bytes from the database as well.
    """
    if tool not in _SCREENSHOT_MODEL_TOOLS or not isinstance(tool_result, dict):
        return {}
    payload = _find_image_payload(tool_result)
    url = payload.get("url", "")
    if url.startswith(("http://", "https://")):
        return {"url": url}
    data_url = payload.get("data_url", "")
    if not data_url.startswith("data:image/"):
        data_url = _image_path_to_data_url(payload.get("path", ""))
    if not data_url.startswith("data:image/"):
        return {}
    return {"data_url": data_url}


def _find_screenshot_result_payload(value: object, depth: int = 0) -> Dict[str, object]:
    if depth > 5:
        return {}
    if isinstance(value, dict):
        if any(key in value for key in ("send_to_user", "bot_send_to_user", "deliver_to_user", "save_to_server", "server_path")):
            return value
        for key in ("result", "payload", "data", "screenshot_result"):
            found = _find_screenshot_result_payload(value.get(key), depth + 1)
            if found:
                return found
        for item in value.values():
            if isinstance(item, (dict, list)):
                found = _find_screenshot_result_payload(item, depth + 1)
                if found:
                    return found
    if isinstance(value, list):
        for item in value:
            found = _find_screenshot_result_payload(item, depth + 1)
            if found:
                return found
    return {}


def _screenshot_send_to_user_enabled(tool: str, tool_result: Dict[str, object], args: Optional[dict] = None) -> bool:
    if tool not in {"browser_screenshot", "screen.capture", "screen.capture_region", "vision.capture", "vision.capture_mouse"}:
        return False
    if isinstance(args, dict) and any(
        key in args and args.get(key) is False
        for key in ("send_to_user", "bot_send_to_user", "deliver_to_user")
    ):
        return False
    payload = _find_screenshot_result_payload(tool_result)
    return (
        payload.get("send_to_user") is True
        or payload.get("bot_send_to_user") is True
        or payload.get("deliver_to_user") is True
        or tool in {"browser_screenshot", "screen.capture", "screen.capture_region", "vision.capture", "vision.capture_mouse"}
    )


def _bot_target_from_route(route: object) -> Dict[str, object]:
    target: Dict[str, object] = {}
    if hasattr(route, "receive_id"):
        target["receive_id"] = str(getattr(route, "receive_id", "") or "")
        target["receive_id_type"] = str(getattr(route, "receive_id_type", "") or "")
    if hasattr(route, "target_id"):
        target["target_id"] = str(getattr(route, "target_id", "") or "")
        target["target_type"] = str(getattr(route, "target_type", "") or "")
    if hasattr(route, "source_message_id"):
        target["msg_id"] = str(getattr(route, "source_message_id", "") or "")
    if hasattr(route, "source_event_id"):
        target["event_id"] = str(getattr(route, "source_event_id", "") or "")
    if hasattr(route, "next_msg_seq"):
        try:
            target["msg_seq"] = max(1, int(getattr(route, "next_msg_seq") or 1))
        except Exception:
            pass
    return target


def _deliver_screenshot_to_bot(bg: Session, message: ChatMessage, *, tool_result: Dict[str, object]) -> Dict[str, object]:
    payload = _find_screenshot_result_payload(tool_result)
    image_payload = _find_image_payload(tool_result)
    media_path = str(payload.get("server_path") or image_payload.get("path") or "").strip()
    media_url = str(payload.get("image_url") or payload.get("public_url") or image_payload.get("url") or "").strip()
    if not media_path and not media_url:
        return {"delivered": False, "reason": "no media path or url"}
    from connector_runtime.bots.messaging import MediaPayload, Recipient, dispatcher
    from connector_runtime.bots.registry import iter_bots

    media = MediaPayload(
        url=media_url,
        path=media_path,
        media_type="image",
        file_name=str(payload.get("file_name") or "screenshot.png"),
    )

    bots = list(iter_bots())
    for bot in bots:
        route = bot.load_session_route(bg, message)
        if not route:
            continue
        target = _bot_target_from_route(route)
        sent = dispatcher.send_media(
            user_id=int(message.user_id),
            ai_config_id=message.ai_config_id,
            channel=bot.channel,
            media=media,
            raw_target=target,
        ).detail
        if hasattr(route, "row") and "msg_seq" in target:
            try:
                route.row.next_msg_seq = int(target["msg_seq"]) + 1
                route.row.updated_at = time.time()
                bg.add(route.row)
                bg.commit()
            except Exception:
                logger.debug("screenshot bot route sequence bump skipped", exc_info=True)
        return {"delivered": True, "mode": "session_route", "channel": bot.channel, "target": target, "result": sent}

    cfg = bg.get(AssistantAIConfig, int(message.ai_config_id or 0)) if message.ai_config_id else None
    channel = str(getattr(cfg, "bot_channel", "") or "").strip().lower()
    active_bot = next((bot for bot in bots if bot.channel == channel), None)
    if active_bot is None:
        return {"delivered": False, "reason": "no bot session route and no active bot channel", "channel": channel}
    if cfg is not None and not active_bot.is_enabled(cfg):
        return {"delivered": False, "reason": "active bot is disabled", "channel": active_bot.channel}
    sent = dispatcher.send_media(
        user_id=int(message.user_id),
        ai_config_id=message.ai_config_id,
        channel=active_bot.channel,
        media=media,
        recipient=Recipient(),
    ).detail
    return {"delivered": True, "mode": "default_target", "channel": active_bot.channel, "result": sent}


def _model_visible_tool_result(
    tool: str,
    tool_result: Dict[str, object],
    *,
    image_attached: bool = True,
) -> object:
    result_payload = tool_result.get("result", tool_result) if isinstance(tool_result, dict) else tool_result
    if tool not in _SCREENSHOT_MODEL_TOOLS or not isinstance(result_payload, dict):
        return result_payload
    cleaned = _omit_image_fields(_sanitize_large_media(result_payload))
    if not isinstance(cleaned, dict):
        cleaned = {}
    cleaned["screenshot_attached_to_model"] = image_attached
    if not image_attached:
        cleaned["instruction"] = (
            "The screenshot could not be attached because the current model does not support image input. "
            "Continue from non-image tool data, use a non-visual tool, or explain the limitation."
        )
        return cleaned
    if tool == "mouse.click":
        cleaned["instruction"] = (
            "The pre-click confirmation image is attached in the next user message. "
            "The click has not been executed yet. Inspect the red marker. If it is not on the intended clickable target center, "
            "estimate correction_dx/correction_dy in pixels and call mouse.click again with corrected x/y to get a new confirmation image. "
            "If the marker is correct, call mouse.click again with confirmed:true to execute the click."
        )
    else:
        cleaned["instruction"] = "The screenshot image is attached in the next user message. Analyze the image directly; do not ask the user to open a local path."
    return cleaned


def _append_mcp_disabled_feedback(
    *,
    bg: Session,
    convo: List[Dict],
    user_id: int,
    ai_config_id: Optional[int],
    ai_kind: str,
    session_id: str,
    session_name: str,
    model: str,
    tool: str,
    arguments: dict,
    native_tool_call_id: str = "",
) -> None:
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
        bg,
        user_id,
        ChatMessageCreate(
            role="user",
            content=notice,
            tags="system_notice_mcp_disabled",
            ai_config_id=ai_config_id,
            ai_kind=ai_kind,
            session_id=session_id,
            session_name=session_name,
            model=model,
            total_tokens=0,
        ),
    )
    if native_tool_call_id:
        convo.append({
            "role": "tool",
            "tool_call_id": native_tool_call_id,
            "content": _safe_json(payload),
        })
    else:
        convo.append({"role": "user", "content": f"{notice}\n\n[工具检查结果]\n{_safe_json(payload)}"})


def _run_worker(
    *,
    run_id: str,
    user_id: int,
    ai_config_id: Optional[int],
    ai_kind: str,
    session_id: str,
    session_name: str,
    model_user_content: Optional[str] = None,
    merged_system_prompt: Optional[str] = None,
    max_steps: Optional[int] = None,
    current_user_message_id: Optional[int] = None,
):
    """Public worker entry. Wraps the implementation with heartbeat lifecycle
    so every caller (monolith thread, ai-runtime dispatcher, scheduler) gets
    watchdog protection without needing to spawn its own heartbeat thread.
    """
    import threading as _threading
    from api.runtime import heartbeat as _hb

    _stop_hb = _threading.Event()

    def _tick_loop() -> None:
        while not _stop_hb.is_set():
            try:
                _hb.tick(run_id)
            except Exception:
                pass
            if _stop_hb.wait(_hb.TICK_INTERVAL_SECONDS):
                return

    _hb_thread = _threading.Thread(target=_tick_loop, name=f"hb-{run_id}", daemon=True)
    _hb_thread.start()

    # Mirror the live answer to a QQ streaming message when applicable. The
    # session (if any) owns final delivery for this run, so we finalize it in
    # the finally block regardless of how the run ends.
    try:
        from connector_runtime.bots.qq.stream_sender import maybe_start_qq_stream
        maybe_start_qq_stream(
            run_id=run_id,
            user_id=user_id,
            ai_config_id=ai_config_id,
            ai_kind=ai_kind,
            session_id=session_id,
        )
    except Exception:
        pass

    try:
        _run_worker_impl(
            run_id=run_id,
            user_id=user_id,
            ai_config_id=ai_config_id,
            ai_kind=ai_kind,
            session_id=session_id,
            session_name=session_name,
            model_user_content=model_user_content,
            merged_system_prompt=merged_system_prompt,
            max_steps=max_steps,
            current_user_message_id=current_user_message_id,
        )
    finally:
        try:
            from connector_runtime.bots.qq.stream_sender import finish_qq_stream
            finish_qq_stream(run_id, session_id=session_id)
        except Exception:
            pass
        _stop_hb.set()
        _hb_thread.join(timeout=1.0)


def _run_worker_impl(
    *,
    run_id: str,
    user_id: int,
    ai_config_id: Optional[int],
    ai_kind: str,
    session_id: str,
    session_name: str,
    model_user_content: Optional[str] = None,
    merged_system_prompt: Optional[str] = None,
    max_steps: Optional[int] = None,
    current_user_message_id: Optional[int] = None,
):
    if _run_should_stop(run_id):
        _run_set_status(run_id, "stopped", finished=True)
        return
    _run_set_status(run_id, "running")
    _set_run_live_meta(
        run_id,
        user_id=user_id,
        ai_config_id=ai_config_id,
        ai_kind=ai_kind,
        session_id=session_id,
        session_name=session_name,
    )
    _ai_debug_stage(
        "START",
        f"{_ai_short_run_id(run_id)} u={user_id} cfg={ai_config_id if ai_config_id is not None else '-'} "
        f"kind={ai_kind} sess={_ai_short(session_id, 24)}",
        "36",
    )
    try:
        with Session(engine) as bg:
            user = bg.get(User, user_id)
            if not user:
                raise RuntimeError("User not found")
            max_steps = _coerce_max_steps(
                max_steps,
                _coerce_max_steps(getattr(user, "mcp_max_steps", DEFAULT_CHAT_MAX_STEPS), DEFAULT_CHAT_MAX_STEPS),
            )
            from api.services.knowledge import kb_store

            cfg, api_key, base_url, model, system_prompt = _resolve_ai_runtime(bg, user, ai_kind, ai_config_id)
            # 方案 A：系统提示 / 人格自动控制直接读 KnowledgeBase 文件（缺失回退 DB）。
            mcp_warning_template = kb_store.effective_system_value(
                user_id, "mcp_format_error_hint", getattr(user, "mcp_format_error_hint", "")
            ).strip()
            auto_ctl = normalize_system_auto_control(
                kb_store.effective_auto_control_json(user_id, cfg) if cfg else None
            )
            compression_failed = False
            task_payload = _load_task_payload_by_session(bg, user_id, ai_config_id, session_id)
            task_job = _load_task_job_by_session(bg, user_id, ai_config_id, session_id)
            is_task_runtime = bool(task_payload) or str(session_id or "").startswith("session_task_")
            # Token-limit override is independent of the tool allow-list / prompt
            # assembly below, so it is resolved here and left out of the shared builder.
            token_threshold_override = None
            if task_payload:
                override_token = task_payload.get("override_token_limit")
                if isinstance(override_token, dict) and bool(override_token.get("enabled")):
                    try:
                        token_threshold_override = max(1, int(override_token.get("value") or 1))
                    except Exception:
                        token_threshold_override = None

            # Single source of truth shared with the live /system-prompt-preview
            # endpoint: identical MCP discovery hint + task sections, so the prompt
            # shown to the user is exactly the prompt the model receives. The MCP
            # tool catalog itself is no longer injected here — the web client
            # attaches it per-turn inside the user message (CLIENT_MCP_CATALOG_MARKER).
            system_prompt, effective_tool_allowlist = build_runtime_system_prompt_and_tools(
                bg,
                user,
                ai_kind=ai_kind,
                ai_config_id=ai_config_id,
                session_id=session_id,
                merged_system_prompt=merged_system_prompt,
                cfg=cfg,
                base_system_prompt=system_prompt,
                task_payload=task_payload,
            )

            msg_stmt = select(ChatMessage).where(
                ChatMessage.user_id == user_id,
                ChatMessage.session_id == session_id,
                ChatMessage.ai_kind == ai_kind,
            ).order_by(ChatMessage.created_at.asc())
            if ai_config_id is not None:
                msg_stmt = msg_stmt.where(ChatMessage.ai_config_id == ai_config_id)
            history = bg.exec(msg_stmt).all()
            convo = [{"role": "system", "content": system_prompt}]
            for m in history:
                tags = str(getattr(m, "tags", "") or "")
                if "system_notice_ai_error" in tags or "system_notice_ai_context_repaired" in tags:
                    continue
                # Messages folded into a conversation summary are excluded from
                # the model context; the summary itself (conversation_summary)
                # stays as a normal user message below.
                if "compressed_away" in tags:
                    continue
                if m.role in ("user", "assistant"):
                    # Deliberately do NOT replay prior assistant turns' reasoning
                    # (``m.think``) back to the model. Re-sending historical
                    # ``reasoning_content`` makes reasoning models restate earlier
                    # thinking, so each new reply's deep-thinking accumulates all
                    # the previous ones and grows unboundedly. Only the in-flight
                    # tool-call turn keeps its reasoning (appended later in this
                    # loop with its tool_calls).
                    convo.append({"role": m.role, "content": m.content})
                elif m.role == "system":
                    if tags.startswith("ai_message_inbound:") or tags.startswith("task_completion_notice:"):
                        convo.append({"role": "user", "content": m.content})
                    elif "phase_summary" in tags:
                        # The per-phase progress anchor: it carries which phase
                        # finished + its status line. The phase's verbose turns are
                        # compressed_away (intended), but this compact summary must
                        # survive a run rebuild so the model knows phases already
                        # done and does not re-plan from phase 1. Replayed as the
                        # user role to match the live injection (see plan.phase_complete
                        # handler) and avoid mid-conversation system messages.
                        convo.append({"role": "user", "content": m.content})
            if model_user_content:
                for i in range(len(convo) - 1, -1, -1):
                    if convo[i].get("role") == "user":
                        convo[i] = {"role": "user", "content": model_user_content}
                        break

            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
            last_rejected_tool_sig = ""
            rejected_repeat = 0
            consecutive_ai_errors = 0
            image_input_disabled = False

            # Native tool schemas are exposed progressively. Keep the full
            # allowlist as the execution boundary, but initially show only MCP
            # self-inspection tools to the model.
            mcp_active = bool(cfg and cfg.mcp_enabled and effective_tool_allowlist)
            exposed_tool_allowlist = set(MCP_INTROSPECTION_TOOLS) & set(effective_tool_allowlist)
            if is_task_runtime:
                # Pre-expose the task / planned-flow tools so a task runtime can
                # plan, advance phases and finish without a describe_tool detour.
                exposed_tool_allowlist |= set(TASK_RUNTIME_REQUIRED_TOOLS) & set(effective_tool_allowlist)
            provider = _detect_provider(base_url)
            _ai_debug_stage(
                "INIT",
                f"{_ai_short_run_id(run_id)} {provider} {model} host={_ai_short_base_url(base_url)} "
                f"hist={len(history)} tools={len(effective_tool_allowlist)}/{len(exposed_tool_allowlist)} "
                f"mcp={'on' if mcp_active else 'off'}",
                "34",
            )

            # Expose session context to MCP tools (e.g. admin.dispatch_task) so
            # async desktop-agent results can be appended to this session. The
            # worker runs in its own thread, so the contextvar is naturally scoped.
            set_run_session_context({
                "user_id": user_id,
                "ai_config_id": ai_config_id,
                "ai_kind": ai_kind,
                "session_id": session_id,
                "session_name": session_name,
                "model": model,
                "current_user_message_id": current_user_message_id,
            })

            pending_ai_reply_message_id = ""
            # Planned task flow state. ``plan_state`` is non-None only while an
            # active plan exists for this (ai, session); it drives per-phase
            # context compaction. ``phase_start_convo_index`` marks where the
            # current phase begins in the live convo; ``phase_started_at`` marks
            # the wall-clock boundary used to tag persisted messages on phase
            # completion; ``phase_mcp_statuses`` records each tool's outcome so a
            # finished phase collapses to a compact status line.
            plan_state = None
            phase_start_convo_index = len(convo)
            phase_started_at = time.time()
            phase_mcp_statuses: List[tuple] = []
            # Plan mode is optional for task runtimes. If the AI chooses to
            # enter a plan, the system drives phase transitions and finish
            # handling from that point onward.
            flow_awaiting_finish = False
            flow_nudges = 0
            # A directive to inject *after* the current tool result is appended
            # (so a native tool_call keeps its matching tool response adjacent).
            pending_flow_directive = ""
            # Load any active plan for this (ai, session). A plan can exist in a
            # normal conversation too — not only a task runtime — so the lookup
            # is unconditional. Without it, a rebuilt run (fresh worker call or
            # post-compression) loses the plan thread and the model re-plans from
            # phase 1 (which abandons the live plan via plan.create).
            if ai_config_id is not None:
                try:
                    plan_state = plan_service.get_active_plan(
                        bg, user_id, int(ai_config_id), session_id
                    )
                    flow_awaiting_finish = plan_service.awaiting_finish(bg, plan_state)
                except Exception:
                    logger.exception("plan state load failed")
                    plan_state = None

            def _inject_flow_directive(target_convo: List[Dict]) -> None:
                """Re-anchor the model on the active plan: append the current
                phase directive (or the finish notice when every phase is done).

                Used both when (re)building a run and right after a context
                compaction, so the model always knows the current phase and never
                falls back to re-planning from phase 1. No-op without an active
                plan; the optional plan-required nudge stays task-runtime only."""
                if plan_state is None:
                    return
                # Re-anchor the full plan skeleton first (goal + every phase's
                # status), then the active directive. Without the overview a
                # rebuilt run only sees the current phase and loses the rest of
                # the plan it had registered via plan.create.
                _prog = plan_service.plan_progress(bg, plan_state)
                _overview = phase_context.render_plan_overview(_prog)
                if _overview:
                    target_convo.append({"role": "user", "content": _overview})
                if flow_awaiting_finish:
                    target_convo.append({
                        "role": "user",
                        "content": phase_context.render_finish_required_notice(plan_state.goal),
                    })
                    return
                _cur = next(
                    (p for p in _prog["phases"] if p["seq"] == plan_state.current_phase_seq),
                    None,
                )
                target_convo.append({
                    "role": "user",
                    "content": phase_context.render_phase_directive(_cur, _prog["phase_count"]),
                })

            def _auto_finalize_plan(final_phase_since_ts: float) -> None:
                """Close the whole plan automatically once its last phase is done.

                The AI no longer has to spend a separate ``plan.finish`` turn:
                completing the final phase *is* the finish. The outcome is derived
                from the phase statuses and a summary is synthesized from each
                phase's own summary, then the durable success/failure log is
                written, the linked task job is completed and knowledge review is
                triggered — mirroring the ``plan.finish`` tool's side effects."""
                nonlocal plan_state, flow_awaiting_finish
                if plan_state is None:
                    return
                now_ts = time.time()
                try:
                    _prog = plan_service.plan_progress(bg, plan_state)
                except Exception:
                    logger.exception("auto plan finalize: progress load failed")
                    _prog = {"phases": [], "goal": getattr(plan_state, "goal", "") or ""}
                _phases = _prog.get("phases") or []
                _outcome = "failure" if any(str(p.get("status")) == "failed" for p in _phases) else "success"
                _summary_lines = ["计划各阶段已全部完成，系统自动收尾。"]
                for _p in _phases:
                    _title = str(_p.get("title") or f"阶段{int(_p.get('seq', 0)) + 1}")
                    _s = str(_p.get("summary") or "").strip()
                    _summary_lines.append(f"- {_title}：{_s or '（无小结）'}")
                _summary = "\n".join(_summary_lines)
                # Fold any still-verbose final-phase turns out of the persisted
                # context (usually a no-op: plan.phase_complete already folded them).
                try:
                    phase_context.mark_phase_messages_compressed(
                        bg,
                        user_id=user_id,
                        ai_config_id=ai_config_id,
                        ai_kind=ai_kind,
                        session_id=session_id,
                        since_ts=final_phase_since_ts,
                        until_ts=now_ts,
                    )
                except Exception:
                    logger.exception("auto plan finalize: compaction tagging failed")
                _log_path = ""
                try:
                    plan_service.finish_plan(bg, plan_state, outcome=_outcome, summary=_summary)
                    _phases_orm = plan_service.list_phases(bg, plan_state.plan_id)
                    try:
                        _log_path = plan_service.write_outcome_log(
                            get_project_root(user_id, int(ai_config_id)),
                            plan_state,
                            _phases_orm,
                            summary=_summary,
                        )
                    except Exception:
                        logger.exception("auto plan finalize: outcome log write failed")
                except Exception:
                    logger.exception("auto plan finalize: finish_plan failed")
                try:
                    from api.services.knowledge.knowledge_review_trigger import trigger_plan_knowledge_review

                    trigger_plan_knowledge_review(
                        user_id=user_id,
                        executor_ai_config_id=int(ai_config_id),
                        goal=_prog.get("goal") or "",
                        outcome=_outcome,
                        summary=_summary,
                        phases=_phases,
                        log_path=_log_path,
                    )
                except Exception:
                    logger.exception("auto plan finalize: knowledge review trigger failed")
                _next_loop_job = None
                if task_job is not None and str(getattr(task_job, "status", "") or "").strip() not in {
                    "completed", "cancelled", "stopped", "error",
                }:
                    try:
                        task_job.status = "completed"
                        task_job.finished_at = now_ts
                        task_job.updated_at = now_ts
                        bg.add(task_job)
                        try:
                            notify_task_completion(
                                user_id=user_id,
                                job_id=str(task_job.job_id or ""),
                                summary=_summary,
                            )
                        except Exception:
                            logger.exception("auto plan finalize: completion notify failed")
                        _next_loop_job = _create_loop_scheduled_job(bg, task_job, now_ts)
                    except Exception:
                        logger.exception("auto plan finalize: task job completion failed")
                _notice_lines = [
                    "[系统提示]",
                    f"所有阶段已完成，计划已自动收尾，结果：{'成功' if _outcome == 'success' else '失败'}。",
                ]
                if _log_path:
                    _notice_lines.append(
                        f"- 完整流程已写入{'成功' if _outcome == 'success' else '失败'}日志: {_log_path}"
                    )
                if _next_loop_job is not None:
                    _notice_lines.append(f"- 循环任务已创建: {_next_loop_job.job_id}")
                try:
                    _save_message(
                        bg,
                        user_id,
                        ChatMessageCreate(
                            role="system",
                            content="\n".join(_notice_lines),
                            tags="system_notice_task_complete",
                            ai_config_id=ai_config_id,
                            ai_kind=ai_kind,
                            session_id=session_id,
                            session_name=session_name,
                            model=model,
                            total_tokens=0,
                        ),
                    )
                    bg.commit()
                except Exception:
                    logger.exception("auto plan finalize: notice persist failed")
                    bg.rollback()
                # Plan is closed; drop the flow state so nothing forces plan.finish.
                plan_state = None
                flow_awaiting_finish = False

            # Tell the AI where it stands. With an active plan, re-anchor on the
            # current phase. Without one, only a task runtime is nudged to plan
            # (plan mode stays optional/AI-driven in normal conversations).
            # A resumed run whose plan already closed its last phase is finalized
            # here rather than forcing a separate plan.finish turn.
            _was_awaiting_finish_on_load = flow_awaiting_finish
            if flow_awaiting_finish:
                _auto_finalize_plan(phase_started_at)
            if plan_state is not None:
                _inject_flow_directive(convo)
            elif is_task_runtime and ai_config_id is not None and not _was_awaiting_finish_on_load:
                convo.append({"role": "user", "content": phase_context.render_plan_required_notice()})

            def _flow_allowed_tool(tool_name: str) -> bool:
                """Hard gate: which tools the planned flow permits right now."""
                if plan_state is None:
                    return True
                name = str(tool_name or "")
                if name in MCP_INTROSPECTION_TOOLS:
                    return True
                if flow_awaiting_finish:
                    return name == "plan.finish"
                return True

            for step_index in range(max_steps):
                if _run_should_stop(run_id):
                    _run_set_status(run_id, "stopped", finished=True)
                    return

                # 抢占式钩子：若收件箱有 pending 的 AI 间消息，在下一次 LLM
                # 调用前把它注入到 convo 强制 AI 优先回复。
                # 必须按 session_id 严格匹配——保证 (AI, session) 的消息流
                # 不会跨会话相互串话。
                if ai_config_id is not None:
                    try:
                        _inbound = ai_message_service.pop_pending_for(
                            user_id, int(ai_config_id), session_id
                        )
                    except Exception as _iex:
                        _inbound = None
                        logger.exception("inbox poll failed")
                    if _inbound is not None:
                        _from_name = _resolve_ai_name_safe(bg, _inbound.from_ai_config_id) or f"AI-{_inbound.from_ai_config_id}"
                        _target_name = _resolve_ai_name_safe(bg, ai_config_id) or f"AI-{ai_config_id}"
                        _requires_reply = bool(getattr(_inbound, "require_reply", True))
                        _msg_type = _normalize_ai_message_type(
                            getattr(_inbound, "message_type", None),
                            _requires_reply,
                        )
                        _injected = _render_ai_message_system_prompt(
                            from_ai_name=_from_name,
                            from_ai_config_id=_inbound.from_ai_config_id,
                            target_ai_name=_target_name,
                            target_ai_config_id=ai_config_id,
                            message_id=_inbound.message_id,
                            current_session_id=session_id,
                            content=_inbound.content,
                            message_type=_msg_type,
                            require_reply=_requires_reply,
                        )
                        _save_message(
                            bg,
                            user_id,
                            ChatMessageCreate(
                                role="user",
                                content=_injected,
                                tags=f"ai_message_inbound:{_msg_type}:{_inbound.message_id}",
                                ai_config_id=ai_config_id,
                                ai_kind=ai_kind,
                                session_id=session_id,
                                session_name=session_name,
                                model=model,
                                total_tokens=0,
                            ),
                        )
                        convo.append({"role": "user", "content": _injected})
                        if _requires_reply or _msg_type == "inquiry":
                            pending_ai_reply_message_id = str(_inbound.message_id or "").strip()
                        else:
                            pending_ai_reply_message_id = ""

                _append_missing_tool_responses(
                    convo,
                    "Synthetic tool result inserted before request because the previous tool call did not receive a tool response.",
                )
                if image_input_disabled:
                    removed_images = _degrade_image_messages_to_text(convo)
                    if removed_images:
                        convo.append({
                            "role": "user",
                            "content": _image_input_degraded_feedback(
                                "The current model previously rejected image input in this run.",
                                removed_images,
                            ),
                        })
                if mcp_active:
                    current_exposed_tools = set(exposed_tool_allowlist) & set(effective_tool_allowlist)
                    current_exposed_tools.update(set(MCP_INTROSPECTION_TOOLS) & set(effective_tool_allowlist))
                    # If plan mode is already active, narrow the visible tools
                    # to the phase-forward action (plus self-inspection).
                    if is_task_runtime and plan_state is None:
                        # Allow knowledge tools so the AI can consult the KB
                        # before committing to a plan structure.
                        _pre_plan_kb_tools = {
                            "knowledge.search",
                            "librarian.consult",
                            "librarian.list_topics",
                        }
                        current_exposed_tools = (
                            {"plan.create"} | set(MCP_INTROSPECTION_TOOLS) | _pre_plan_kb_tools
                        ) & set(effective_tool_allowlist)
                    elif is_task_runtime and flow_awaiting_finish:
                        current_exposed_tools = (
                            {"plan.finish"} | set(MCP_INTROSPECTION_TOOLS)
                        ) & set(effective_tool_allowlist)
                    step_tools, native_tool_name_map = _build_native_tools_payload(current_exposed_tools)
                else:
                    step_tools, native_tool_name_map = [], {}
                start_at = time.time()
                _ai_debug_stage(
                    "TURN",
                    f"{_ai_short_run_id(run_id)} #{step_index + 1}/{max_steps} "
                    f"start msgs={len(convo)} tools={len(step_tools)} "
                    f"reply={'y' if pending_ai_reply_message_id else 'n'}",
                    "33",
                )
                try:
                    if provider == "anthropic":
                        sr = stream_turn_anthropic(
                            run_id=run_id,
                            base_url=base_url,
                            api_key=api_key,
                            model=model,
                            convo=convo,
                            step_tools=step_tools,
                            native_tool_name_map=native_tool_name_map,
                        )
                    else:
                        oa_payload = {
                            "model": model,
                            "messages": convo,
                            "stream": True,
                            "stream_options": {"include_usage": True},
                        }
                        if step_tools:
                            oa_payload["tools"] = step_tools
                            oa_payload["tool_choice"] = "auto"
                            # The worker executes one MCP action at a time. If
                            # an OpenAI-compatible model emits parallel tool
                            # calls, some providers reject the next request
                            # unless every call id is answered. Ask the model
                            # for sequential calls at the protocol level too.
                            oa_payload["parallel_tool_calls"] = False
                        response = ai_http_post(base_url, headers=headers, json=oa_payload, timeout=300, stream=True)
                        if not response.ok and "parallel_tool_calls" in oa_payload:
                            unsupported_hint = str(response.text or "").lower()
                            if "parallel_tool_calls" in unsupported_hint and (
                                "unsupported" in unsupported_hint
                                or "unknown" in unsupported_hint
                                or "invalid" in unsupported_hint
                                or "extra" in unsupported_hint
                            ):
                                oa_payload.pop("parallel_tool_calls", None)
                                response.close()
                                response = ai_http_post(base_url, headers=headers, json=oa_payload, timeout=300, stream=True)
                        _raise_for_upstream_error(response)
                        sr = stream_turn_openai_compat(
                            run_id=run_id,
                            response=response,
                            native_tool_name_map=native_tool_name_map,
                        )
                    consecutive_ai_errors = 0
                except Exception as ai_exc:
                    consecutive_ai_errors += 1
                    error_text = _extract_mcp_error(ai_exc)
                    _ai_debug_stage(
                        "ERR",
                        f"{_ai_short_run_id(run_id)} #{step_index + 1}/{max_steps} "
                        f"x{consecutive_ai_errors} {_ai_short(error_text, 140)}",
                        "31",
                    )
                    repaired_ids = _append_missing_tool_responses(convo, error_text)
                    if repaired_ids:
                        consecutive_ai_errors = 0
                        _save_message(
                            bg,
                            user_id,
                            ChatMessageCreate(
                                role="system",
                                content="\n".join([
                                    "[AI 对话上下文已修复]",
                                    "已补齐缺失的 tool 响应，避免上游接口因 tool_calls 上下文不完整而拒绝请求。",
                                    f"补齐 tool_call_id: {', '.join(repaired_ids)}",
                                ]),
                                tags="system_notice_ai_context_repaired",
                                ai_config_id=ai_config_id,
                                ai_kind=ai_kind,
                                session_id=session_id,
                                session_name=session_name,
                                model=model,
                                total_tokens=0,
                            ),
                        )
                        _set_run_live_phase(run_id, "generating")
                        continue
                    if _is_image_input_unsupported_error(error_text):
                        removed_images = _degrade_image_messages_to_text(convo)
                        if removed_images:
                            image_input_disabled = True
                            consecutive_ai_errors = 0
                            convo.append({
                                "role": "user",
                                "content": _image_input_degraded_feedback(error_text, removed_images),
                            })
                            notice = "\n".join([
                                "[AI 对话出错]",
                                error_text,
                                "",
                                f"检测到当前模型不支持图片输入；系统已移除 {removed_images} 张图片。",
                                "该错误已作为运行时消息发送给 AI，对话将继续执行。",
                            ])
                            _save_message(
                                bg,
                                user_id,
                                ChatMessageCreate(
                                    role="system",
                                    content=notice,
                                    tags="system_notice_ai_error",
                                    ai_config_id=ai_config_id,
                                    ai_kind=ai_kind,
                                    session_id=session_id,
                                    session_name=session_name,
                                    model=model,
                                    total_tokens=0,
                                ),
                            )
                            _set_run_live_phase(run_id, "generating")
                            continue
                    notice_lines = [
                        "[AI 对话出错]",
                        error_text,
                        "",
                        f"连续错误次数: {consecutive_ai_errors}/3",
                    ]
                    if consecutive_ai_errors < 3:
                        notice_lines.extend([
                            "",
                            "系统将重试上游请求；该错误不会作为 user 消息发送给 AI。",
                        ])
                    notice = "\n".join(notice_lines)
                    _save_message(
                        bg,
                        user_id,
                        ChatMessageCreate(
                            role="system",
                            content=notice,
                            tags="system_notice_ai_error",
                            ai_config_id=ai_config_id,
                            ai_kind=ai_kind,
                            session_id=session_id,
                            session_name=session_name,
                            model=model,
                            total_tokens=0,
                        ),
                    )
                    _set_run_live_phase(run_id, "generating")
                    if consecutive_ai_errors >= 3:
                        _run_set_status(run_id, "error", f"AI request failed 3 times consecutively: {error_text}", finished=True)
                        return
                    continue

                if sr.stopped:
                    _run_set_status(run_id, "stopped", finished=True)
                    return
                if _run_should_stop(run_id):
                    _run_set_status(run_id, "stopped", finished=True)
                    return

                assistant_text = sr.assistant_text
                reasoning_content = sr.reasoning_content
                usage = sr.usage
                finish_reason = sr.finish_reason
                payload_call = sr.payload_call
                _has_native_tc = sr.has_native_tc
                _tc_id = sr.tc_id
                _tc_name = sr.tc_name
                _tc_args = sr.tc_args
                latency = time.time() - start_at
                token_triplet = (
                    f"{int(usage.get('prompt_tokens') or 0)}/"
                    f"{int(usage.get('completion_tokens') or 0)}/"
                    f"{int(usage.get('total_tokens') or 0)}"
                )
                tc_name = _tc_name or (payload_call or {}).get("tool") or "-"
                _ai_debug_stage(
                    "DONE",
                    f"{_ai_short_run_id(run_id)} #{step_index + 1}/{max_steps} "
                    f"{finish_reason or 'stop'} {int(latency * 1000)}ms tok={token_triplet} "
                    f"tc={'native:' if _has_native_tc else ''}{_ai_short(tc_name, 32)}",
                    "32",
                )
                if not payload_call and not _has_native_tc:
                    payload_call = _extract_first_mcp_call(assistant_text)
                assistant_tags = "mcp_assistant_call" if (payload_call or _has_native_tc) else ""

                saved = _save_message(
                    bg,
                    user_id,
                    ChatMessageCreate(
                        role="assistant",
                        content=assistant_text,
                        think=reasoning_content or None,
                        tags=assistant_tags,
                        ai_config_id=ai_config_id,
                        ai_kind=ai_kind,
                        session_id=session_id,
                        session_name=session_name,
                        model=model,
                        prompt_tokens=int(usage.get("prompt_tokens") or 0),
                        completion_tokens=int(usage.get("completion_tokens") or 0),
                        total_tokens=int(usage.get("total_tokens") or 0),
                        cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0) or None,
                        system_prompt=system_prompt,
                        finish_reason=finish_reason,
                        latency=latency,
                    ),
                )
                if _has_native_tc and _tc_name:
                    assistant_item = {
                        "role": "assistant",
                        "content": assistant_text or None,
                        "tool_calls": [{
                            "id": _tc_id or "call_0",
                            "type": "function",
                            "function": {"name": _tc_name, "arguments": _tc_args},
                        }],
                    }
                    if reasoning_content:
                        assistant_item["reasoning_content"] = reasoning_content
                    convo.append(assistant_item)
                else:
                    assistant_item = {"role": "assistant", "content": assistant_text}
                    if reasoning_content:
                        assistant_item["reasoning_content"] = reasoning_content
                    convo.append(assistant_item)
                _set_run_live_text(run_id, "")
                _set_run_live_usage(run_id, 0, 0, 0)

                # Text-based fallback: if no payload_call was detected during streaming.
                if not payload_call and not _has_native_tc:
                    payload_call = _extract_first_mcp_call(assistant_text)
                payload_tool = str((payload_call or {}).get("tool") or "").strip()
                if payload_call and payload_tool in native_tool_name_map:
                    payload_tool = native_tool_name_map[payload_tool]
                    payload_call["tool"] = payload_tool
                # AI 主动压缩上下文：复用自动压缩机制，但不看 token 阈值，立即把
                # 较早的对话历史折叠成摘要并重建当前会话，让本轮就用上压缩结果。
                _payload_args = (payload_call or {}).get("arguments", {}) or {}
                _payload_action = str(_payload_args.get("action") or "").strip().lower()
                if payload_tool == "conversation.manage" and _payload_action == "compress":
                    _compress_args = _payload_args
                    try:
                        _keep_recent = int(_compress_args.get("keep_recent", 4))
                    except Exception:
                        _keep_recent = 4
                    _keep_recent = max(0, min(_keep_recent, 20))
                    rebuilt_convo = None
                    try:
                        rebuilt_convo = conversation_compress.compress_session(
                            bg,
                            convo=convo,
                            user_id=user_id,
                            ai_config_id=ai_config_id,
                            ai_kind=ai_kind,
                            session_id=session_id,
                            session_name=session_name,
                            model=model,
                            api_key=api_key,
                            base_url=base_url,
                            system_prompt=system_prompt,
                            compression_prompt=str(auto_ctl.get("compression_prompt") or ""),
                            session_tokens=0,
                            threshold=0,
                            keep_recent=_keep_recent,
                        )
                    except Exception:
                        logger.exception("conversation.compress (manual) failed")
                        rebuilt_convo = None
                    if rebuilt_convo:
                        _compress_note = (
                            "已压缩上下文：较早的对话历史已折叠为摘要，最近若干条原样保留，"
                            "请基于压缩后的上下文继续。"
                        )
                        _save_message(
                            bg,
                            user_id,
                            ChatMessageCreate(
                                role="user",
                                content=_compress_note,
                                tags="system_notice_compress",
                                ai_config_id=ai_config_id,
                                ai_kind=ai_kind,
                                session_id=session_id,
                                session_name=session_name,
                                model=model,
                                total_tokens=0,
                            ),
                        )
                        convo = rebuilt_convo
                        convo.append({"role": "user", "content": _compress_note})
                        _set_run_live_usage(run_id, 0, 0, 0)
                        # Conversation was rebuilt; reset the phase boundary so
                        # phase compaction never splices against a stale index.
                        if plan_state is not None:
                            phase_start_convo_index = len(convo)
                            phase_started_at = time.time()
                            phase_mcp_statuses = []
                    else:
                        _compress_note = "当前对话历史较短，暂无需压缩，请直接继续。"
                        if _has_native_tc:
                            convo.append({
                                "role": "tool",
                                "tool_call_id": _tc_id or "call_0",
                                "content": _safe_json(
                                    {"success": True, "compressed": False, "note": _compress_note}
                                ),
                            })
                        else:
                            convo.append({"role": "user", "content": _compress_note})
                    _set_run_live_phase(run_id, "generating")
                    continue
                joined_native_tools = (
                    _split_concatenated_native_tool_name(payload_tool, native_tool_name_map)
                    if payload_call
                    else []
                )
                if is_task_runtime:
                    latest_task_job = _load_task_job_by_session(bg, user_id, ai_config_id, session_id)
                    if latest_task_job:
                        task_job = latest_task_job

                # Automatic conversation compression: when a digital member's
                # session token count reaches its threshold, summarize the older
                # part of the conversation into a compact summary and CONTINUE the
                # same session (no new generation, no agent death).
                task_is_finished = bool(task_job and _is_task_finished_status(str(task_job.status or "")))
                if (
                    cfg
                    and cfg.ai_role == "digital_member"
                    and not task_is_finished
                    and not compression_failed
                    and payload_tool not in ("plan.phase_complete", "plan.finish")
                ):
                    threshold = token_threshold_override if token_threshold_override is not None else max(1, int(cfg.token_limit or 1))
                    session_tokens = _session_total_tokens(bg, user_id, ai_kind, session_id, ai_config_id)
                    if session_tokens >= threshold:
                        rebuilt_convo = None
                        try:
                            rebuilt_convo = conversation_compress.compress_session(
                                bg,
                                convo=convo,
                                user_id=user_id,
                                ai_config_id=ai_config_id,
                                ai_kind=ai_kind,
                                session_id=session_id,
                                session_name=session_name,
                                model=model,
                                api_key=api_key,
                                base_url=base_url,
                                system_prompt=system_prompt,
                                compression_prompt=str(auto_ctl.get("compression_prompt") or ""),
                                session_tokens=session_tokens,
                                threshold=threshold,
                            )
                        except Exception:
                            logger.exception("conversation_compress.compress_session failed")
                            rebuilt_convo = None
                        if rebuilt_convo:
                            convo = rebuilt_convo
                            # Reset live pending token state so the threshold is not
                            # immediately re-hit on the next loop step.
                            _set_run_live_usage(run_id, 0, 0, 0)
                            if plan_state is not None:
                                # Compression rebuilt convo as system+summary+recent
                                # and dropped the live phase directive. Re-anchor the
                                # model on the current phase so it does not re-plan
                                # from phase 1 after the context shrinks.
                                phase_start_convo_index = len(convo)
                                _inject_flow_directive(convo)
                                phase_started_at = time.time()
                                phase_mcp_statuses = []
                            continue
                        # Compression failed or not enough history to compress:
                        # don't retry-loop forever; fall through this turn.
                        compression_failed = True

                # Plan-mode gate: once a plan exists, reject tool calls that do
                # not move the current plan forward. The assistant tool_call
                # message was already appended above, so we answer it (native)
                # / reply (text) and steer back.
                if payload_call and is_task_runtime and plan_state is not None and not _flow_allowed_tool(payload_tool):
                    if plan_state is None:
                        _flow_block_text = phase_context.render_plan_required_notice()
                    elif flow_awaiting_finish:
                        _flow_block_text = phase_context.render_finish_required_notice(
                            plan_state.goal if plan_state else ""
                        )
                    else:
                        _flow_block_text = phase_context.render_continue_phase_notice()
                    if _has_native_tc:
                        convo.append({
                            "role": "tool",
                            "tool_call_id": _tc_id or "call_0",
                            "content": _safe_json({
                                "success": False,
                                "error": "flow_violation",
                                "note": _flow_block_text,
                            }),
                        })
                    else:
                        convo.append({"role": "user", "content": _flow_block_text})
                    _set_run_live_phase(run_id, "generating")
                    continue

                if not payload_call:
                    # Only check for text-format MCP warnings when not using native tool_calls.
                    if not _has_native_tc:
                        warning = _build_mcp_stream_warning(assistant_text, cfg, mcp_warning_template)
                        if warning:
                            _save_message(
                                bg,
                                user_id,
                                ChatMessageCreate(
                                    role="user",
                                    content=warning,
                                    tags="system_notice_mcp_format_invalid",
                                    ai_config_id=ai_config_id,
                                    ai_kind=ai_kind,
                                    session_id=session_id,
                                    session_name=session_name,
                                    model=model,
                                    total_tokens=0,
                                ),
                            )
                            convo.append({"role": "user", "content": warning})
                            continue
                    # In plan mode, a task runtime should not end by "talking".
                    # Steer it back to the active plan flow. ``flow_nudges``
                    # bounds this so a stubborn model degrades to a normal
                    # finish instead of burning the whole step budget.
                    if is_task_runtime and plan_state is not None and flow_nudges < 3:
                        if plan_state is None:
                            _nudge_text = phase_context.render_plan_required_notice()
                        elif flow_awaiting_finish:
                            _nudge_text = phase_context.render_finish_required_notice(
                                plan_state.goal if plan_state else ""
                            )
                        else:
                            _nudge_text = phase_context.render_continue_phase_notice()
                        flow_nudges += 1
                        convo.append({"role": "user", "content": _nudge_text})
                        _set_run_live_phase(run_id, "generating")
                        continue
                    if pending_ai_reply_message_id and assistant_text.strip() and ai_config_id is not None:
                        try:
                            _auto_reply = ai_message_service.complete_inbound_with_assistant_reply(
                                message_id=pending_ai_reply_message_id,
                                user_id=user_id,
                                replier_ai_config_id=int(ai_config_id),
                                content=assistant_text,
                            )
                            if _auto_reply and _auto_reply.get("auto_completed"):
                                saved.tags = _append_mcp_state_to_tags(
                                    saved.tags,
                                    "ai.auto_reply",
                                    {"message_id": pending_ai_reply_message_id},
                                    "assistant final text delivered as AI message reply",
                                )
                                bg.add(saved)
                                bg.commit()
                        except Exception as _arex:
                            logger.exception("auto AI message reply failed")
                        finally:
                            pending_ai_reply_message_id = ""
                    # Auto-finalize simple (non-plan) task jobs when the run ends naturally.
                    # If a plan was created, the AI is required to call plan.finish explicitly
                    # (enforced by flow_awaiting_finish + _flow_allowed_tool).
                    if task_job is not None and str(getattr(task_job, "status", "") or "").strip() not in {"completed", "cancelled", "stopped", "error"}:
                        try:
                            active_plan = None
                            if ai_config_id is not None:
                                active_plan = plan_service.get_active_plan(bg, user_id, int(ai_config_id), session_id)
                            if active_plan is None:
                                finished_at = time.time()
                                task_job.status = "completed"
                                task_job.finished_at = finished_at
                                task_job.updated_at = finished_at
                                bg.add(task_job)
                                try:
                                    notify_task_completion(
                                        user_id=user_id,
                                        job_id=str(task_job.job_id or ""),
                                        summary="任务执行完成（简单任务，无计划流程）。",
                                    )
                                except Exception:
                                    logger.exception("auto simple task completion notify failed")
                                try:
                                    _create_loop_scheduled_job(bg, task_job, time.time())
                                except Exception:
                                    logger.exception("auto simple task loop schedule failed")
                                try:
                                    bg.commit()
                                except Exception:
                                    pass
                        except Exception:
                            logger.exception("auto finalize simple task job failed")
                    _run_set_status(run_id, "completed", finished=True)
                    return
                if joined_native_tools:
                    joined_mcp_tools = [native_tool_name_map.get(item, item) for item in joined_native_tools]
                    tool = payload_tool
                    arguments = payload_call.get("arguments", {}) or {}
                    if cfg and not cfg.mcp_enabled:
                        denied_sig = f"mcp_disabled|{tool}|{json.dumps(arguments, ensure_ascii=False, sort_keys=True)}"
                        if denied_sig == last_rejected_tool_sig:
                            rejected_repeat += 1
                        else:
                            last_rejected_tool_sig = denied_sig
                            rejected_repeat = 1
                        _append_mcp_disabled_feedback(
                            bg=bg,
                            convo=convo,
                            user_id=user_id,
                            ai_config_id=ai_config_id,
                            ai_kind=ai_kind,
                            session_id=session_id,
                            session_name=session_name,
                            model=model,
                            tool=tool,
                            arguments=arguments,
                            native_tool_call_id=_tc_id or "call_0" if _has_native_tc else "",
                        )
                        if rejected_repeat >= 3:
                            _run_set_status(run_id, "error", "Repeated MCP call while MCP is disabled", finished=True)
                            return
                        _set_run_live_phase(run_id, "generating")
                        continue

                    compound_results = []
                    compound_failed = False
                    for split_tool in joined_mcp_tools:
                        if _run_should_stop(run_id):
                            _run_set_status(run_id, "stopped", finished=True)
                            return
                        item_failed = False
                        item_error = _joined_tool_skip_reason(split_tool, arguments, effective_tool_allowlist)
                        if item_error:
                            item_failed = True
                            compound_failed = True
                            item_result = {"result": {"success": False, "error": item_error}}
                            item_result_text = _build_mcp_display_result(
                                split_tool,
                                item_result,
                                ok=False,
                                error_message=item_error,
                            )
                        else:
                            _set_run_live_phase(run_id, "waiting_mcp", split_tool)
                            try:
                                item_result = run_async(_call_mcp_or_endpoint_tool(split_tool, user_id, arguments, ai_config_id))
                                endpoint_failed, endpoint_error = _tool_result_failed(item_result)
                                if endpoint_failed:
                                    item_failed = True
                                    compound_failed = True
                                    item_error = endpoint_error
                                    item_result_text = _build_mcp_display_result(
                                        split_tool,
                                        item_result,
                                        ok=False,
                                        error_message=item_error,
                                    )
                                else:
                                    item_result_text = _build_mcp_display_result(split_tool, item_result, ok=True)
                            except Exception as mcp_exc:
                                item_failed = True
                                compound_failed = True
                                item_error = _extract_mcp_error(mcp_exc)
                                item_result = {"result": {"success": False, "error": item_error}}
                                item_result_text = _build_mcp_display_result(
                                    split_tool,
                                    item_result,
                                    ok=False,
                                    error_message=item_error,
                                )
                        _record_mcp_call(
                            tool=split_tool, user_id=user_id, ai_config_id=ai_config_id,
                            session_id=session_id, run_id=run_id, message_id=getattr(saved, "id", None),
                            failed=item_failed, error=item_error,
                        )
                        if plan_state is not None:
                            phase_context.record_status(phase_mcp_statuses, split_tool, item_failed)
                        saved.tags = _append_mcp_state_to_tags(
                            saved.tags,
                            split_tool,
                            arguments,
                            item_result_text,
                        )
                        bg.add(saved)
                        bg.commit()
                        item_screenshot_ref = {} if item_failed else _screenshot_display_ref(split_tool, item_result)
                        _save_mcp_tool_bubble(
                            bg,
                            user_id=user_id,
                            ai_config_id=ai_config_id,
                            ai_kind=ai_kind,
                            session_id=session_id,
                            session_name=session_name,
                            model=model,
                            tool=split_tool,
                            arguments=arguments,
                            result_text=item_result_text,
                            failed=item_failed,
                            image_url=item_screenshot_ref.get("url", ""),
                            image_data_url=item_screenshot_ref.get("data_url", ""),
                            tool_result=item_result if isinstance(item_result, dict) else None,
                        )
                        compound_results.append({
                            "tool": split_tool,
                            "failed": item_failed,
                            "error": item_error,
                            "result": item_result.get("result", item_result),
                        })

                    compound_payload = {
                        "success": not compound_failed,
                        "compat_mode": "split_concatenated_tool_names",
                        "original_tool": tool,
                        "tools": compound_results,
                    }
                    if _has_native_tc:
                        convo.append({
                            "role": "tool",
                            "tool_call_id": _tc_id or "call_0",
                            "content": _safe_json(compound_payload),
                        })
                    else:
                        follow_up = (
                            "[MCP兼容处理完成]\n"
                            "系统检测到多个 MCP 工具名被拼接，已按顺序拆分处理。\n"
                            "其中安全且参数完整的工具已执行；缺少参数或不适合从拼接调用执行的工具已逐项标记失败。\n\n"
                            "[工具处理结果]\n"
                            f"{_safe_json(compound_payload)}\n\n"
                            "请基于以上结果继续；如仍需调用失败的工具，请按标准格式一次调用一个 MCP 工具并提供所需参数。"
                        )
                        convo.append({"role": "user", "content": follow_up})
                    _set_run_live_phase(run_id, "generating")
                    continue
                if _run_should_stop(run_id):
                    _run_set_status(run_id, "stopped", finished=True)
                    return
                if cfg and not cfg.mcp_enabled:
                    tool = payload_call.get("tool", "")
                    arguments = payload_call.get("arguments", {}) or {}
                    denied_sig = f"mcp_disabled|{tool}|{json.dumps(arguments, ensure_ascii=False, sort_keys=True)}"
                    if denied_sig == last_rejected_tool_sig:
                        rejected_repeat += 1
                    else:
                        last_rejected_tool_sig = denied_sig
                        rejected_repeat = 1
                    _append_mcp_disabled_feedback(
                        bg=bg,
                        convo=convo,
                        user_id=user_id,
                        ai_config_id=ai_config_id,
                        ai_kind=ai_kind,
                        session_id=session_id,
                        session_name=session_name,
                        model=model,
                        tool=tool,
                        arguments=arguments,
                        native_tool_call_id=_tc_id or "call_0" if _has_native_tc else "",
                    )
                    if rejected_repeat >= 3:
                        _run_set_status(run_id, "error", "Repeated MCP call while MCP is disabled", finished=True)
                        return
                    _set_run_live_phase(run_id, "generating")
                    continue

                tool = payload_call.get("tool", "")
                arguments = payload_call.get("arguments", {}) or {}
                if _run_should_stop(run_id):
                    _run_set_status(run_id, "stopped", finished=True)
                    return
                if tool not in effective_tool_allowlist:
                    denied_sig = f"{tool}|{json.dumps(arguments, ensure_ascii=False, sort_keys=True)}"
                    if denied_sig == last_rejected_tool_sig:
                        rejected_repeat += 1
                    else:
                        last_rejected_tool_sig = denied_sig
                        rejected_repeat = 1
                    tool_failed = True
                    tool_error = f"Tool not allowed for this task: {tool}"
                    tool_result = {"result": {"success": False, "error": tool_error}}
                    result_text = _build_mcp_display_result(tool, tool_result, ok=False, error_message=tool_error)
                    saved.tags = _append_mcp_state_to_tags(saved.tags, tool, arguments, result_text)
                    bg.add(saved)
                    bg.commit()
                    _save_mcp_tool_bubble(
                        bg,
                        user_id=user_id,
                        ai_config_id=ai_config_id,
                        ai_kind=ai_kind,
                        session_id=session_id,
                        session_name=session_name,
                        model=model,
                        tool=tool,
                        arguments=arguments,
                        result_text=result_text,
                        failed=True,
                    )
                    if _has_native_tc:
                        convo.append({
                            "role": "tool",
                            "tool_call_id": _tc_id or "call_0",
                            "content": json.dumps({"error": tool_error, "allowed_tools": sorted(effective_tool_allowlist)}, ensure_ascii=False),
                        })
                    else:
                        follow_up = (
                            "[MCP执行失败]\n"
                            f"工具 `{tool}` 未在当前任务允许范围内。\n"
                            f"可用工具: {', '.join(sorted(effective_tool_allowlist)) or '（空）'}\n"
                            "请改用任务允许的 MCP 工具继续执行。"
                        )
                        convo.append({"role": "user", "content": follow_up})
                    if rejected_repeat >= 3:
                        _run_set_status(run_id, "error", f"Repeated disallowed MCP tool call: {tool}", finished=True)
                        return
                    continue
                _set_run_live_phase(run_id, "waiting_mcp", tool)
                tool_failed = False
                tool_error = ""
                try:
                    tool_result = run_async(_call_mcp_or_endpoint_tool(tool, user_id, arguments, ai_config_id))
                    endpoint_failed, endpoint_error = _tool_result_failed(tool_result)
                    if endpoint_failed:
                        tool_failed = True
                        tool_error = endpoint_error
                        result_text = _build_mcp_display_result(tool, tool_result, ok=False, error_message=tool_error)
                    else:
                        result_text = _build_mcp_display_result(tool, tool_result, ok=True)
                except Exception as mcp_exc:
                    tool_failed = True
                    tool_error = _extract_mcp_error(mcp_exc)
                    tool_result = {"result": {"success": False, "error": tool_error}}
                    result_text = _build_mcp_display_result(tool, tool_result, ok=False, error_message=tool_error)
                _record_mcp_call(
                    tool=tool, user_id=user_id, ai_config_id=ai_config_id,
                    session_id=session_id, run_id=run_id, message_id=getattr(saved, "id", None),
                    failed=tool_failed, error=tool_error,
                )
                if plan_state is not None:
                    phase_context.record_status(phase_mcp_statuses, tool, tool_failed)
                result_payload = tool_result.get("result", tool_result) if isinstance(tool_result, dict) else {}
                if (
                    (not tool_failed)
                    and tool == "conversation.manage"
                    and isinstance(result_payload, dict)
                    and result_payload.get("action") == "rename"
                    and str(result_payload.get("session_id") or "") == str(session_id)
                ):
                    renamed_session = str(result_payload.get("name") or "").strip()
                    if renamed_session:
                        session_name = renamed_session
                        saved.session_name = renamed_session
                saved.tags = _append_mcp_state_to_tags(saved.tags, tool, arguments, result_text)
                bg.add(saved)
                bg.commit()
                screenshot_ref = {} if tool_failed else _screenshot_display_ref(tool, tool_result)
                _save_mcp_tool_bubble(
                    bg,
                    user_id=user_id,
                    ai_config_id=ai_config_id,
                    ai_kind=ai_kind,
                    session_id=session_id,
                    session_name=session_name,
                    model=model,
                    tool=tool,
                    arguments=arguments,
                    result_text=result_text,
                    failed=tool_failed,
                    image_url=screenshot_ref.get("url", ""),
                    image_data_url=screenshot_ref.get("data_url", ""),
                    tool_result=tool_result if isinstance(tool_result, dict) else None,
                )

                if (not tool_failed) and tool == "mcp.describe_tool":
                    described_payload = tool_result.get("result", tool_result) if isinstance(tool_result, dict) else {}
                    described_names: list[str] = []
                    if isinstance(described_payload, dict):
                        batch = described_payload.get("tools")
                        if isinstance(batch, list):
                            described_names = [
                                str((item or {}).get("name") or "").strip()
                                for item in batch
                                if isinstance(item, dict)
                            ]
                        else:
                            described_names = [str(described_payload.get("name") or "").strip()]
                    for described_tool in described_names:
                        if described_tool and described_tool in effective_tool_allowlist:
                            exposed_tool_allowlist.add(described_tool)

                if (
                    (not tool_failed)
                    and tool == "conversation.manage"
                    and isinstance(result_payload, dict)
                    and result_payload.get("action") == "clear"
                    and str(result_payload.get("session_id") or "") == str(session_id)
                ):
                    current_user_content = _load_current_user_content(
                        bg,
                        user_id=user_id,
                        ai_config_id=ai_config_id,
                        ai_kind=ai_kind,
                        session_id=session_id,
                        current_user_message_id=current_user_message_id,
                        fallback=model_user_content,
                    )
                    _reset_convo_after_clear(
                        convo,
                        system_prompt=system_prompt,
                        current_user_content=current_user_content,
                        tool_result=tool_result,
                    )
                    if plan_state is not None:
                        phase_start_convo_index = len(convo)
                        phase_started_at = time.time()
                        phase_mcp_statuses = []
                    _set_run_live_phase(run_id, "generating")
                    continue

                # Planned task flow (system-driven). ``plan.create`` is followed
                # by the system handing the AI phase 1; ``plan.phase_complete`` folds
                # the finished phase out of context and the system hands over the
                # next phase (or requires plan.finish); ``plan.finish`` closes the
                # whole plan run. Simple (non-plan) tasks complete naturally without
                # an explicit completion tool call.
                if (not tool_failed) and tool == "plan.create":
                    # Answer the tool call, then drive straight into phase 1 — the
                    # AI never has to poll plan progress itself.
                    if _has_native_tc:
                        convo.append({
                            "role": "tool",
                            "tool_call_id": _tc_id or "call_0",
                            "content": _safe_json(_model_visible_tool_result(tool, tool_result, image_attached=False)),
                        })
                    else:
                        convo.append({
                            "role": "user",
                            "content": (
                                f"[MCP执行确认]\n系统已执行工具：{tool}\n执行状态：成功\n\n"
                                "[工具执行结果]\n"
                                f"{_safe_json(_model_visible_tool_result(tool, tool_result, image_attached=False))}"
                            ),
                        })
                    try:
                        plan_state = plan_service.get_active_plan(
                            bg, user_id, int(ai_config_id), session_id
                        ) if ai_config_id is not None else None
                        flow_awaiting_finish = plan_service.awaiting_finish(bg, plan_state)
                    except Exception:
                        logger.exception("plan reload after plan.create failed")
                        plan_state = None
                    flow_nudges = 0
                    phase_start_convo_index = len(convo)
                    phase_started_at = time.time()
                    phase_mcp_statuses = []
                    if plan_state is not None and not flow_awaiting_finish:
                        _prog = plan_service.plan_progress(bg, plan_state)
                        _cur = next(
                            (p for p in _prog["phases"] if p["seq"] == plan_state.current_phase_seq),
                            None,
                        )
                        convo.append({
                            "role": "user",
                            "content": phase_context.render_phase_directive(_cur, _prog["phase_count"]),
                        })
                    _set_run_live_phase(run_id, "generating")
                    continue

                if (not tool_failed) and tool == "plan.phase_complete" and plan_state is not None:
                    result_payload = tool_result.get("result", tool_result) if isinstance(tool_result, dict) else {}
                    finished_phase = result_payload.get("finished_phase") if isinstance(result_payload, dict) else None
                    boundary = max(0, min(phase_start_convo_index, len(convo)))
                    compaction_text = phase_context.build_phase_compaction_text(
                        finished_phase, phase_mcp_statuses
                    )
                    # Fold the finished phase out of the live conversation: drop
                    # its deep-thinking + verbose MCP results, keep one status line.
                    # Injected as the user role (not system) so it stays consistent
                    # with the other plan-mode directives and avoids mid-conversation
                    # system messages that some providers reject.
                    convo[boundary:] = [{"role": "user", "content": compaction_text}]
                    now_ts = time.time()
                    try:
                        phase_context.mark_phase_messages_compressed(
                            bg,
                            user_id=user_id,
                            ai_config_id=ai_config_id,
                            ai_kind=ai_kind,
                            session_id=session_id,
                            since_ts=phase_started_at,
                            until_ts=now_ts,
                        )
                        _save_message(
                            bg,
                            user_id,
                            ChatMessageCreate(
                                role="system",
                                content=compaction_text,
                                tags="phase_summary",
                                ai_config_id=ai_config_id,
                                ai_kind=ai_kind,
                                session_id=session_id,
                                session_name=session_name,
                                model=model,
                                total_tokens=max(1, len(compaction_text) // 3),
                            ),
                        )
                        bg.commit()
                    except Exception:
                        logger.exception("phase compaction persistence failed")
                        bg.rollback()
                    # Refresh plan state and open a fresh boundary for next phase.
                    try:
                        plan_state = plan_service.get_active_plan(
                            bg, user_id, int(ai_config_id), session_id
                        ) if ai_config_id is not None else None
                        flow_awaiting_finish = plan_service.awaiting_finish(bg, plan_state)
                    except Exception:
                        logger.exception("plan reload after plan.phase_complete failed")
                    flow_nudges = 0
                    phase_start_convo_index = len(convo)
                    phase_started_at = time.time()
                    phase_mcp_statuses = []
                    # System drives the next step: hand over the next phase, or —
                    # when every phase is done — finalize the whole plan on its own.
                    # Completing the last phase IS the finish; the AI does not need
                    # a separate, forced plan.finish turn.
                    if flow_awaiting_finish:
                        _auto_finalize_plan(phase_started_at)
                        _set_run_live_phase(run_id, "idle")
                        _run_set_status(run_id, "completed", finished=True)
                        return
                    elif plan_state is not None:
                        _prog = plan_service.plan_progress(bg, plan_state)
                        _cur = next(
                            (p for p in _prog["phases"] if p["seq"] == plan_state.current_phase_seq),
                            None,
                        )
                        convo.append({
                            "role": "user",
                            "content": phase_context.render_phase_directive(_cur, _prog["phase_count"]),
                        })
                    _set_run_live_phase(run_id, "generating")
                    continue

                if (not tool_failed) and tool == "plan.finish":
                    result_payload = tool_result.get("result", tool_result) if isinstance(tool_result, dict) else {}
                    finish_outcome = str(result_payload.get("outcome") or "").strip() or "success"
                    finish_job_id = str(result_payload.get("job_id") or "").strip()
                    finish_log_path = str(result_payload.get("log_path") or "").strip()
                    finish_summary = str((arguments or {}).get("summary") or "").strip()
                    # Hide the whole run's deep-thinking + MCP detail: the final
                    # phase (since its boundary) still carries verbose turns;
                    # earlier phases were already folded at plan.phase_complete.
                    now_ts = time.time()
                    completed_job = None
                    if finish_job_id and ai_config_id is not None:
                        completed_job = bg.exec(
                            select(AITaskJob).where(
                                AITaskJob.user_id == user_id,
                                AITaskJob.ai_config_id == ai_config_id,
                                AITaskJob.job_id == finish_job_id,
                            )
                        ).first()
                    if completed_job is None and task_job is not None:
                        completed_job = task_job
                    try:
                        phase_context.mark_phase_messages_compressed(
                            bg,
                            user_id=user_id,
                            ai_config_id=ai_config_id,
                            ai_kind=ai_kind,
                            session_id=session_id,
                            since_ts=phase_started_at,
                            until_ts=now_ts,
                        )
                    except Exception:
                        logger.exception("plan.finish compaction tagging failed")
                    if completed_job is not None:
                        completed_job.status = "completed"
                        completed_job.finished_at = now_ts
                        completed_job.updated_at = now_ts
                        bg.add(completed_job)
                        try:
                            notify_task_completion(
                                user_id=user_id,
                                job_id=str(completed_job.job_id or ""),
                                summary=finish_summary,
                            )
                        except Exception:
                            logger.exception("plan.finish completion notify failed")
                    next_loop_job = _create_loop_scheduled_job(bg, completed_job, time.time())
                    finish_notice_lines = [
                        "[系统提示]",
                        f"任务已通过 `plan.finish` 收尾，结果：{'成功' if finish_outcome == 'success' else '失败'}。",
                    ]
                    if finish_log_path:
                        finish_notice_lines.append(
                            f"- 完整流程已写入{'成功' if finish_outcome == 'success' else '失败'}日志: {finish_log_path}"
                        )
                    if next_loop_job is not None:
                        finish_notice_lines.append(f"- 循环任务已创建: {next_loop_job.job_id}")
                    finish_notice_lines.append("")
                    finish_notice_lines.append("本任务对话已自动锁定，不再继续后续操作。")
                    _save_message(
                        bg,
                        user_id,
                        ChatMessageCreate(
                            role="system",
                            content="\n".join(finish_notice_lines),
                            tags="system_notice_task_complete",
                            ai_config_id=ai_config_id,
                            ai_kind=ai_kind,
                            session_id=session_id,
                            session_name=session_name,
                            model=model,
                            total_tokens=0,
                        ),
                    )
                    bg.commit()
                    _set_run_live_phase(run_id, "idle")
                    _run_set_status(run_id, "completed", finished=True)
                    return

                else:
                    screenshot_message = _browser_screenshot_image_message(tool, tool_result)
                    attach_screenshot = bool(screenshot_message) and not image_input_disabled
                    if attach_screenshot:
                        _prune_prior_runtime_screenshot_images(convo)
                    if _has_native_tc:
                        # Native path: use tool role so model sees structured result.
                        convo.append({
                            "role": "tool",
                            "tool_call_id": _tc_id or "call_0",
                            "content": _safe_json(_model_visible_tool_result(
                                tool,
                                tool_result,
                                image_attached=attach_screenshot,
                            )),
                        })
                        if attach_screenshot:
                            convo.append(screenshot_message)
                    else:
                        follow_up_text = (
                            f"[MCP执行{'失败' if tool_failed else '确认'}]\n"
                            f"系统已执行工具：{tool}\n"
                            f"执行状态：{'失败' if tool_failed else '成功'}\n\n"
                            "[工具参数]\n"
                            f"{_safe_json(arguments)}\n\n"
                            "[工具执行结果]\n"
                            f"{_safe_json(_model_visible_tool_result(tool, tool_result, image_attached=attach_screenshot))}\n\n"
                            "请基于以上结果继续完成任务。"
                        )
                        if attach_screenshot:
                            convo.append({
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": follow_up_text},
                                    *screenshot_message["content"],
                                ],
                            })
                        else:
                            convo.append({"role": "user", "content": follow_up_text})
                _set_run_live_phase(run_id, "generating")

            notice = (
                "[系统提示]\n"
                f"本轮已达到 MCP 连续执行步数上限（{max_steps}）。"
                "系统已暂停本轮自动继续，避免无限循环；如需继续，请发送新消息或提高系统设置里的 MCP 最大步数。"
            )
            _save_message(
                bg,
                user_id,
                ChatMessageCreate(
                    role="system",
                    content=notice,
                    tags="system_notice_mcp_max_steps",
                    ai_config_id=ai_config_id,
                    ai_kind=ai_kind,
                    session_id=session_id,
                    session_name=session_name,
                    model=model,
                    total_tokens=0,
                ),
            )
            _run_set_status(run_id, "completed", finished=True)
    except Exception as exc:
        _run_set_status(run_id, "error", str(exc), finished=True)
