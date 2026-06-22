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

from api.chat_runtime.mcp_parser import MCP_CALL_BLOCK_RE
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
    content = MCP_CALL_BLOCK_RE.sub("", content)
    content = re.sub(r"<mcp[-_]call\b[\s\S]*$", "", content, flags=re.IGNORECASE)
    content = re.sub(r"\n{3,}", "\n\n", content)
    return content.strip()


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


def forward_readiness(cfg) -> "str | None":
    """Why a web-chat forward for ``cfg`` would not deliver, or ``None`` if ready.

    Used by the chat dropdown toggle to turn the otherwise-silent prerequisites
    (bound channel + enabled bot + a configured default receiver) into an
    actionable message.
    """
    channel = str(getattr(cfg, "bot_channel", "") or "").strip().lower()
    bot = next((b for b in iter_bots() if b.channel == channel), None)
    if bot is None:
        return "该 AI 未绑定可用的机器人渠道"
    label = bot.label or channel
    if not bot.is_enabled(cfg):
        return f"{label}机器人未启用，请先在「AI 配置 → 机器人配置」里启用"
    if not bot.has_default_recipient(cfg):
        return f"{label}机器人未配置默认接收方，转发将无处送达；请在机器人配置里设置默认接收方"
    return None


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
    not_ready = forward_readiness(cfg)
    if not_ready:
        # The conversation opted into forwarding but the bot can't deliver —
        # log loudly so this never fails silently.
        logger.warning("forward web chat skipped (session=%s): %s", sid, not_ready)
        return
    channel = str(cfg.bot_channel or "").strip().lower()
    bot = next((b for b in bots if b.channel == channel), None)
    if bot is None:
        return
    try:
        # Empty recipient → adapter falls back to the configured default receiver.
        dispatcher.send_text(
            user_id=int(message.user_id),
            ai_config_id=message.ai_config_id,
            channel=channel,
            text=content,
            recipient=Recipient(),
        )
    except Exception as exc:  # delivery is best-effort, never break the save path
        logger.exception("forward web chat to bot failed message_id=%s: %s", message.id, exc)
