"""Bot-agnostic outbound delivery for newly persisted assistant messages.

``notify_saved_assistant_message`` is the single entry point called from
``services.chat_persistence`` after a saved assistant message has been
committed. We:

1. Strip MCP-call blocks so private tool traffic never leaks to chat UI.
2. Identify which bot owns the message by checking the registered routes.
3. Hand the message to the matching adapter for delivery.

Adding a new bot does not require touching this file — the registry
iteration picks up any new ``BotAdapter`` automatically.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from sqlmodel import select

from api.chat_runtime.mcp_parser import strip_tool_call_blocks
from .base import channel_for_session_id
from .messaging import Recipient, dispatcher
from .registry import iter_bots

if TYPE_CHECKING:
    from sqlmodel import Session

    from api.models import ChatMessage

logger = logging.getLogger(__name__)


def _visible_content(message: "ChatMessage") -> str:
    """Return the assistant content with MCP-call blocks stripped."""
    content = str(message.content or "")
    if not content:
        return ""
    return strip_tool_call_blocks(content)


def _is_ai_error_notice(message: "ChatMessage") -> bool:
    return message.role == "system" and "system_notice_ai_error" in str(message.tags or "")


def _bot_ai_error_notice(message: "ChatMessage") -> str:
    text = str(message.content or "")
    match = re.search(r"\bHTTP\s+(\d{3})\b", text, flags=re.IGNORECASE)
    if not match:
        match = re.search(r"\bstatus\s*[:=]?\s*(\d{3})\b", text, flags=re.IGNORECASE)
    status = match.group(1) if match else "unknown"
    return "\n".join([
        "[AI 对话出错]",
        f"status {status}",
    ])


def _is_visible_assistant_message(message: "ChatMessage") -> bool:
    if message.role != "assistant":
        return False
    return bool(_visible_content(message))


def _is_bot_deliverable_message(message: "ChatMessage") -> bool:
    return _is_visible_assistant_message(message) or _is_ai_error_notice(message)


def _format_content(content: str) -> str:
    raw = str(content or "")
    if not raw:
        return ""
    return raw.strip()


def notify_saved_assistant_message(session: "Session", message: "ChatMessage") -> None:
    """Deliver a saved assistant message to whichever bot owns its session.

    No-op when the message is empty, not from the assistant, or its
    ``session_id`` has no registered bot route.
    """
    if not _is_bot_deliverable_message(message):
        return
    content = _bot_ai_error_notice(message) if _is_ai_error_notice(message) else _visible_content(message)

    bot = None
    route = None
    for candidate in iter_bots():
        route = candidate.load_session_route(session, message)
        if route:
            bot = candidate
            break

    content = content.lstrip("\n")
    content = _format_content(content)

    if bot is None or route is None:
        # No bot owns this session (ordinary web conversation). Optionally
        # mirror the reply into the bound bot's default conversation when the
        # AI config opted in.
        _maybe_forward_web_chat(session, message, content)
        return

    bot.notify_assistant_message(
        session,
        message,
        rendered_content=content,
        route=route,
    )


def _forward_bot(cfg, bots=None):
    """Resolve the preferred ready bot, falling back to another enabled channel."""
    available = list(bots if bots is not None else iter_bots())
    preferred_channel = str(getattr(cfg, "bot_channel", "") or "").strip().lower()
    preferred = next((bot for bot in available if bot.channel == preferred_channel), None)
    candidates = ([preferred] if preferred is not None else []) + [
        bot for bot in available if bot is not preferred
    ]
    enabled = [bot for bot in candidates if bot.is_enabled(cfg)]
    for bot in enabled:
        if bot.has_default_recipient(cfg):
            return bot, None
    if not enabled:
        return None, "该 AI 未启用可用的机器人渠道"
    labels = "、".join((bot.label or bot.channel) for bot in enabled)
    return None, f"{labels}机器人未配置默认接收方，转发将无处送达"


def forward_readiness(cfg) -> "str | None":
    """Why a web-chat forward for ``cfg`` would not deliver, or ``None`` if ready.

    Used by the chat dropdown toggle to turn the otherwise-silent prerequisites
    (bound channel + enabled bot + a configured default receiver) into an
    actionable message.
    """
    _bot, warning = _forward_bot(cfg)
    return warning


def _maybe_forward_web_chat(session: "Session", message: "ChatMessage", content: str) -> None:
    """Forward an ordinary web-chat assistant reply to the bot default receiver.

    Gated per-conversation by ``ChatSession.forward_to_bot`` (set from the chat
    dropdown). Skips bot-owned sessions (already handled by routes) and
    task-runtime sessions (their progress is surfaced in the console).
    """
    if not content:
        return
    sid = str(message.session_id or "")
    if not sid or sid.startswith("session_task_"):
        return
    if message.ai_config_id is None:
        return

    from api.models import AssistantAIConfig, ChatSession

    chat_session = session.exec(
        select(ChatSession).where(
            ChatSession.user_id == message.user_id,
            ChatSession.ai_config_id == message.ai_config_id,
            ChatSession.ai_kind == message.ai_kind,
            ChatSession.session_id == sid,
        )
    ).first()
    if chat_session is None or not bool(getattr(chat_session, "forward_to_bot", False)):
        return

    bots = list(iter_bots())
    # Defensive: if this session actually belongs to a bot, routes own it.
    if channel_for_session_id(sid, bots):
        return
    cfg = session.get(AssistantAIConfig, message.ai_config_id)
    if cfg is None:
        return
    bot, not_ready = _forward_bot(cfg, bots)
    if not_ready:
        # The conversation opted into forwarding but the bot can't deliver —
        # log loudly so this never fails silently.
        logger.warning("forward web chat skipped (session=%s): %s", sid, not_ready)
        return
    if bot is None:
        return
    try:
        # Empty recipient → adapter falls back to the configured default receiver.
        dispatcher.send_text(
            user_id=int(message.user_id),
            ai_config_id=message.ai_config_id,
            channel=bot.channel,
            text=content,
            recipient=Recipient(),
        )
    except Exception as exc:  # delivery is best-effort, never break the save path
        logger.exception("forward web chat to bot failed message_id=%s: %s", message.id, exc)
