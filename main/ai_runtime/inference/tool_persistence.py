"""Persist normalized tool outcomes and deliver screenshot bubbles."""

from dataclasses import dataclass
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from sqlmodel import Session

from api.chat_runtime.chat_prompt_utils import _append_mcp_state_to_tags, _safe_json
from api.models import AssistantAIConfig, ChatMessage, ChatMessageCreate
from api.services.chat.chat_persistence import _save_message
from ai_runtime.inference import tool_media
from ai_runtime.inference import phase_context
from ai_runtime.inference.tool_execution import (
    JoinedToolRequest,
    iter_joined_tool_executions,
)
from connector_runtime.dispatch.desktop_device_tools import is_endpoint_agent_tool
from mcp_runtime.mcp import registry


logger = logging.getLogger(__name__)
_SCREENSHOT_BUBBLE_MARKER = "[截图]"


@dataclass(frozen=True)
class ToolCallRecord:
    tool: str
    user_id: int
    ai_config_id: Optional[int]
    session_id: str
    run_id: str
    message_id: Optional[int]
    failed: bool
    error: str


@dataclass(frozen=True)
class ToolBubbleRequest:
    session: Session
    user_id: int
    ai_config_id: Optional[int]
    ai_kind: str
    session_id: str
    session_name: str
    model: str
    tool: str
    arguments: dict
    result_text: str
    failed: bool = False
    image_url: str = ""
    image_data_url: str = ""
    tool_result: Optional[Dict[str, object]] = None
    latency: Optional[float] = None


@dataclass(frozen=True)
class JoinedPersistenceContext:
    session: Session
    saved_message: ChatMessage
    user_id: int
    ai_config_id: Optional[int]
    ai_kind: str
    session_id: str
    session_name: str
    model: str
    run_id: str
    plan_active: bool
    phase_mcp_statuses: List[tuple]
    should_stop: Callable[[], bool]
    mark_waiting: Callable[[str, Dict[str, Any]], None]


@dataclass(frozen=True)
class JoinedBatchOutcome:
    stopped: bool
    failed: bool
    items: tuple[Dict[str, object], ...]


def record_tool_call(record: ToolCallRecord) -> None:
    """Best-effort failure-rate record with conversation coordinates."""

    try:
        from api.services.mcp import mcp_stats

        mcp_stats.record_call(
            user_id=record.user_id,
            ai_config_id=record.ai_config_id,
            tool=record.tool,
            success=not record.failed,
            error=record.error or "",
            session_id=str(record.session_id or ""),
            run_id=str(record.run_id or ""),
            message_id=record.message_id,
        )
    except Exception:
        pass


def execute_and_persist_joined_batch(
    request: JoinedToolRequest,
    context: JoinedPersistenceContext,
) -> JoinedBatchOutcome:
    items = []
    compound_failed = False
    events = iter_joined_tool_executions(
        request,
        should_stop=context.should_stop,
        mark_waiting=context.mark_waiting,
    )
    for event in events:
        if event.stopped:
            return JoinedBatchOutcome(True, compound_failed, tuple(items))
        execution = event.execution
        if execution is None:
            continue
        compound_failed = compound_failed or execution.failed
        record_tool_call(ToolCallRecord(
            tool=event.tool, user_id=context.user_id,
            ai_config_id=context.ai_config_id, session_id=context.session_id,
            run_id=context.run_id, message_id=getattr(context.saved_message, "id", None),
            failed=execution.failed, error=execution.error,
        ))
        if context.plan_active:
            phase_context.record_status(
                context.phase_mcp_statuses,
                event.tool,
                execution.failed,
            )
        context.saved_message.tags = _append_mcp_state_to_tags(
            context.saved_message.tags,
            event.tool,
            request.arguments,
            execution.display_text,
        )
        context.session.add(context.saved_message)
        context.session.commit()
        screenshot = (
            {}
            if execution.failed
            else tool_media.screenshot_display_ref(event.tool, execution.result)
        )
        save_tool_bubble(ToolBubbleRequest(
            session=context.session, user_id=context.user_id,
            ai_config_id=context.ai_config_id, ai_kind=context.ai_kind,
            session_id=context.session_id, session_name=context.session_name,
            model=context.model, tool=event.tool, arguments=request.arguments,
            result_text=execution.display_text, failed=execution.failed,
            image_url=screenshot.get("url", ""),
            image_data_url=screenshot.get("data_url", ""),
            tool_result=execution.result, latency=execution.latency,
        ))
        items.append({
            "tool": event.tool,
            "failed": execution.failed,
            "error": execution.error,
            "result": execution.result.get("result", execution.result),
        })
    return JoinedBatchOutcome(False, compound_failed, tuple(items))


def _completed_device_id(
    tool: str,
    tool_result: Optional[Dict[str, object]],
) -> tuple[str, str]:
    if not tool_result or registry.has(tool) or not is_endpoint_agent_tool(tool):
        return "", ""
    payload = tool_result.get("result", tool_result)
    if not isinstance(payload, dict):
        return "", ""
    device_id = str(
        payload.get("deviceId") or payload.get("device_id") or ""
    ).strip()
    reported_name = str(
        payload.get("deviceName") or payload.get("device_name") or ""
    ).strip()
    return device_id, " ".join(reported_name.split())


def _connected_device_name(user_id: int, device_id: str) -> str:
    try:
        from api.devices.live import connected_agent_rows_for_user

        for row in connected_agent_rows_for_user(user_id):
            candidate_id = str(
                row.get("id") or row.get("deviceId") or row.get("device_id") or ""
            ).strip()
            if candidate_id == device_id:
                label = str(row.get("remark") or row.get("name") or "").strip()
                return " ".join(label.split()) or device_id
    except Exception:
        logger.debug(
            "endpoint device display-name lookup skipped device=%s",
            device_id,
            exc_info=True,
        )
    return device_id


def tool_device_identity(
    tool: str,
    user_id: int,
    tool_result: Optional[Dict[str, object]],
) -> tuple[str, str]:
    """Resolve identity only from the completed dispatch envelope."""

    device_id, reported_name = _completed_device_id(tool, tool_result)
    if not device_id:
        return "", ""
    if reported_name:
        return device_id, reported_name
    return device_id, _connected_device_name(user_id, device_id)


def build_tool_bubble_content(
    tool: str,
    arguments: dict,
    result_text: str,
    failed: bool = False,
    image_url: str = "",
    *,
    device_id: str = "",
    device_name: str = "",
) -> str:
    status = "失败" if failed else "成功"
    device_meta = (
        f"设备: {device_name or device_id}\n设备号: {device_id}\n"
        if device_id
        else ""
    )
    content = (
        "[MCP工具]\n"
        f"工具: {tool}\n"
        f"状态: {status}\n"
        f"{device_meta}\n"
        "[参数]\n"
        f"{_safe_json(arguments or {})}\n\n"
        "[结果]\n"
        f"{result_text}"
    )
    if image_url:
        content += f"\n\n{_SCREENSHOT_BUBBLE_MARKER}\n{image_url}"
    return content


def extract_screenshot_bubble_url(content: str) -> str:
    marker = f"\n\n{_SCREENSHOT_BUBBLE_MARKER}\n"
    text = str(content or "")
    if marker not in text:
        return ""
    return text.rsplit(marker, 1)[-1].strip().splitlines()[0].strip()


def _bot_target_from_route(route: object) -> Dict[str, object]:
    target: Dict[str, object] = {}
    for source, destination in (
        ("receive_id", "receive_id"),
        ("receive_id_type", "receive_id_type"),
        ("target_id", "target_id"),
        ("target_type", "target_type"),
        ("source_message_id", "msg_id"),
        ("source_event_id", "event_id"),
    ):
        if hasattr(route, source):
            target[destination] = str(getattr(route, source, "") or "")
    if hasattr(route, "next_msg_seq"):
        try:
            target["msg_seq"] = max(1, int(getattr(route, "next_msg_seq") or 1))
        except (TypeError, ValueError):
            pass
    return target


def _send_screenshot_to_bot(
    session: Session,
    message: ChatMessage,
    tool_result: Dict[str, object],
) -> Dict[str, object]:
    from connector_runtime.bots.messaging import MediaPayload, Recipient, dispatcher
    from connector_runtime.bots.registry import iter_bots

    payload = tool_media.find_screenshot_result_payload(tool_result)
    image_payload = tool_media.find_image_payload(tool_result)
    media_path = str(
        payload.get("server_path") or image_payload.get("path") or ""
    ).strip()
    media_url = str(
        payload.get("image_url")
        or payload.get("public_url")
        or image_payload.get("url")
        or ""
    ).strip()
    if not media_path and not media_url:
        return {"delivered": False, "reason": "no media path or url"}
    media = MediaPayload(
        url=media_url,
        path=media_path,
        media_type="image",
        file_name=str(payload.get("file_name") or "screenshot.png"),
    )
    bots = list(iter_bots())
    routed = _send_to_session_route(session, message, bots, media, dispatcher)
    if routed is not None:
        return routed
    return _send_to_default_target(session, message, bots, media, dispatcher, Recipient)


def _send_to_session_route(session, message, bots, media, dispatcher):
    for bot in bots:
        route = bot.load_session_route(session, message)
        if not route:
            continue
        target = _bot_target_from_route(route)
        detail = dispatcher.send_media(
            user_id=int(message.user_id),
            ai_config_id=message.ai_config_id,
            channel=bot.channel,
            media=media,
            raw_target=target,
        ).detail
        if hasattr(route, "row") and "msg_seq" in target:
            _advance_route_sequence(session, route, int(target["msg_seq"]))
        return {
            "delivered": True,
            "mode": "session_route",
            "channel": bot.channel,
            "target": target,
            "result": detail,
        }
    return None


def _advance_route_sequence(session: Session, route, sequence: int) -> None:
    try:
        route.row.next_msg_seq = sequence + 1
        route.row.updated_at = time.time()
        session.add(route.row)
        session.commit()
    except Exception:
        logger.debug("screenshot bot route sequence bump skipped", exc_info=True)


def _send_to_default_target(session, message, bots, media, dispatcher, recipient_type):
    config = (
        session.get(AssistantAIConfig, int(message.ai_config_id or 0))
        if message.ai_config_id
        else None
    )
    channel = str(getattr(config, "bot_channel", "") or "").strip().lower()
    active_bot = next((bot for bot in bots if bot.channel == channel), None)
    if active_bot is None:
        return {
            "delivered": False,
            "reason": "no bot session route and no active bot channel",
            "channel": channel,
        }
    if config is not None and not active_bot.is_enabled(config):
        return {
            "delivered": False,
            "reason": "active bot is disabled",
            "channel": active_bot.channel,
        }
    detail = dispatcher.send_media(
        user_id=int(message.user_id),
        ai_config_id=message.ai_config_id,
        channel=active_bot.channel,
        media=media,
        recipient=recipient_type(),
    ).detail
    return {
        "delivered": True,
        "mode": "default_target",
        "channel": active_bot.channel,
        "result": detail,
    }


def save_tool_bubble(request: ToolBubbleRequest) -> None:
    device_id, device_name = tool_device_identity(
        request.tool,
        request.user_id,
        request.tool_result,
    )
    try:
        from api.services.workflows.recording_service import RecordedToolCall, record_completed_tool_call

        envelope = request.tool_result or {}
        recorded_result = envelope.get("result", envelope) if isinstance(envelope, dict) else envelope
        record_completed_tool_call(request.session, RecordedToolCall(
            user_id=request.user_id, ai_config_id=request.ai_config_id,
            tool=request.tool, arguments=request.arguments, result=recorded_result,
            success=not request.failed, error="tool call failed" if request.failed else "",
            device_id=device_id,
        ))
    except Exception:
        logger.warning("workflow operation recording skipped", exc_info=True)
    message = _save_message(
        request.session,
        request.user_id,
        ChatMessageCreate(
            role="system",
            content=build_tool_bubble_content(
                request.tool,
                request.arguments,
                request.result_text,
                request.failed,
                request.image_url,
                device_id=device_id,
                device_name=device_name,
            ),
            tags="mcp_tool_call",
            ai_config_id=request.ai_config_id,
            ai_kind=request.ai_kind,
            session_id=request.session_id,
            session_name=request.session_name,
            model=request.model,
            total_tokens=0,
            latency=request.latency,
        ),
    )
    _persist_screenshot_data(request, message, device_id, device_name)
    _deliver_screenshot(request, message, device_id, device_name)


def _persist_screenshot_data(request, message, device_id, device_name) -> None:
    if not request.image_data_url or not message.id:
        return
    try:
        from api.services.chat.chat_media import message_media_url, save_message_image_data_url

        media = save_message_image_data_url(
            request.session,
            message=message,
            data_url=request.image_data_url,
        )
        message.content = build_tool_bubble_content(
            request.tool,
            request.arguments,
            request.result_text,
            request.failed,
            message_media_url(media),
            device_id=device_id,
            device_name=device_name,
        )
        request.session.add(message)
        request.session.commit()
    except Exception:
        logger.debug("screenshot chat-media persist skipped", exc_info=True)


def _deliver_screenshot(request, message, device_id, device_name) -> None:
    if (
        request.failed
        or not request.tool_result
        or not tool_media.screenshot_send_to_user_enabled(
            request.tool,
            request.tool_result,
            request.arguments,
        )
    ):
        return
    try:
        delivery = _send_screenshot_to_bot(
            request.session,
            message,
            request.tool_result,
        )
        if not delivery:
            return
        message.content = build_tool_bubble_content(
            request.tool,
            request.arguments,
            f"{request.result_text}\n\n[机器人发送]\n{_safe_json(delivery)}",
            request.failed,
            extract_screenshot_bubble_url(message.content) or request.image_url,
            device_id=device_id,
            device_name=device_name,
        )
        request.session.add(message)
        request.session.commit()
    except Exception:
        logger.debug("screenshot bot delivery skipped", exc_info=True)
