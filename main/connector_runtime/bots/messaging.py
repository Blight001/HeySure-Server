"""Outbound messaging: one typed pipeline for every "bot sends a message" path.

Before this module, three callers (``message.send+to+user``, the saved-message
notifier, the web-chat forward) each built a loose ``target: Dict[str, Any]``
bag, re-resolved the channel, and re-applied the default-receiver fallback;
each adapter then re-unpacked the bag into a per-bot service call. Changing how
a bot sent anything meant touching four files.

This module replaces that with:

* **Value objects** (:class:`Recipient`, :class:`MediaPayload`,
  :class:`DeliveryResult`) — a stable, typed contract instead of dict bags
  (interface-segregation / Liskov: every adapter speaks the same shapes).
* **One orchestrator** (:class:`OutboundDispatcher`) — channel resolution +
  recipient parsing + delivery in a single place (single-responsibility /
  law-of-Demeter). Callers depend only on this façade (dependency-inversion).

Adapters contribute only small primitives (``parse_recipient`` /
``deliver_text`` / ``deliver_media``); the orchestration is composed here once
rather than copied per channel (composition-over-inheritance). Adding a bot or
changing the send flow stays open for extension, closed for modification
(open-closed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional

if TYPE_CHECKING:
    from .base import BotAdapter


@dataclass(frozen=True)
class Recipient:
    """Channel-agnostic addressing for one outbound message.

    Each adapter maps the relevant fields onto its own API: Feishu uses
    ``to_id``/``to_type`` (receive_id / receive_id_type); QQ additionally uses
    the reply context (``reply_message_id`` / ``reply_event_id`` / ``msg_seq``).
    An empty recipient (``to_id == ""``) tells the adapter to fall back to the
    channel's configured default receiver.
    """

    to_id: str = ""
    to_type: str = ""
    reply_message_id: str = ""
    reply_event_id: str = ""
    msg_seq: Optional[int] = None

    @property
    def is_explicit(self) -> bool:
        return bool(self.to_id)


@dataclass(frozen=True)
class MediaPayload:
    """A media attachment plus the optional caption that rides with it."""

    text: str = ""
    url: str = ""
    path: str = ""
    media_type: str = ""
    file_name: str = ""
    duration: Optional[int] = None

    @property
    def has_media(self) -> bool:
        return bool(self.url or self.path)


@dataclass
class DeliveryResult:
    """Uniform result of a dispatch, regardless of channel."""

    ok: bool
    channel: str
    detail: Any = None
    parts: List[Any] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"success": self.ok, "channel": self.channel}
        if self.detail is not None:
            out["result"] = self.detail
        if len(self.parts) > 1:
            out["results"] = self.parts
        return out


class OutboundDispatcher:
    """The single entry point for sending a message through any bot.

    Resolves the channel, asks the adapter to parse addressing into a
    :class:`Recipient`, then delegates the actual wire call to the adapter
    primitive. Holds no channel-specific knowledge of its own.
    """

    def resolve_channel(self, channel: Optional[str], ai_config_id: Optional[int], user_id: int) -> str:
        ch = str(channel or "").strip().lower()
        if ch:
            return ch
        # Fall back to the AI config's bound channel.
        if ai_config_id:
            try:
                from sqlmodel import Session, select

                from api.database import engine
                from api.models import AssistantAIConfig

                with Session(engine) as session:
                    cfg = session.exec(
                        select(AssistantAIConfig).where(
                            AssistantAIConfig.id == ai_config_id,
                            AssistantAIConfig.user_id == user_id,
                        )
                    ).first()
                if cfg:
                    return str(cfg.bot_channel or "feishu").strip().lower()
            except Exception:
                pass
        return "feishu"

    def resolve_bot(self, channel: str) -> Optional["BotAdapter"]:
        from .registry import get
        return get(channel)

    def send_text(
        self,
        *,
        user_id: int,
        ai_config_id: Optional[int],
        text: str,
        channel: Optional[str] = None,
        recipient: Optional[Recipient] = None,
        raw_target: Optional[Mapping[str, Any]] = None,
    ) -> DeliveryResult:
        bot, ch = self._require_bot(channel, ai_config_id, user_id)
        rcpt = recipient if recipient is not None else bot.parse_recipient(raw_target or {})
        detail = bot.deliver_text(
            user_id=user_id, ai_config_id=ai_config_id, recipient=rcpt, text=text
        )
        return DeliveryResult(ok=_result_ok(detail), channel=ch, detail=detail)

    def send_media(
        self,
        *,
        user_id: int,
        ai_config_id: Optional[int],
        media: MediaPayload,
        channel: Optional[str] = None,
        recipient: Optional[Recipient] = None,
        raw_target: Optional[Mapping[str, Any]] = None,
    ) -> DeliveryResult:
        bot, ch = self._require_bot(channel, ai_config_id, user_id)
        rcpt = recipient if recipient is not None else bot.parse_recipient(raw_target or {})
        detail = bot.deliver_media(
            user_id=user_id, ai_config_id=ai_config_id, recipient=rcpt, media=media
        )
        return DeliveryResult(ok=_result_ok(detail), channel=ch, detail=detail)

    def _require_bot(self, channel: Optional[str], ai_config_id: Optional[int], user_id: int):
        from fastapi import HTTPException

        from .registry import all_channels

        ch = self.resolve_channel(channel, ai_config_id, user_id)
        bot = self.resolve_bot(ch)
        if bot is None:
            raise HTTPException(
                status_code=400,
                detail=f"channel '{ch}' not supported; use one of {sorted(all_channels())}",
            )
        return bot, ch


def _result_ok(detail: Any) -> bool:
    if isinstance(detail, dict):
        if "success" in detail:
            return bool(detail.get("success"))
        if "results" in detail and isinstance(detail["results"], list):
            return all(_result_ok(item) for item in detail["results"])
    return True


#: Process-wide singleton; callers import this rather than constructing one.
dispatcher = OutboundDispatcher()
