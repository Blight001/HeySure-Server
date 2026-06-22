"""Abstract base class every bot adapter must implement.

Each :class:`BotAdapter` is a thin façade over a per-bot ``service`` /
``long_connection`` module. Cross-cutting code (gateway lifespan,
connector keepalive, notify dispatcher, status routes) talks to bots
through this interface only.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Dict, Iterable, Mapping, Optional, Set

if TYPE_CHECKING:
    from sqlmodel import Session

    from api.models import AssistantAIConfig, ChatMessage
    from .messaging import MediaPayload, Recipient


class BotAdapter(ABC):
    """Uniform interface a bot plugin exposes to the rest of the server.

    Concrete subclasses are expected to be cheap singletons — one instance
    per supported channel. The instance is registered with
    :func:`bots.register` at import time and is then reused everywhere.
    """

    #: Stable identifier used by ``AssistantAIConfig.bot_channel`` and by
    #: ``session_id`` prefixes (e.g. ``"feishu"`` -> ``"feishu_<id>_..."``).
    channel: str = ""

    #: Human-facing label shown in admin UI / diagnostics.
    label: str = ""

    #: Prefix used on ``ChatMessage.session_id`` to identify rows that came
    #: from this bot. ``""`` disables prefix-based routing.
    session_prefix: str = ""

    # ---- config / enablement ------------------------------------------------

    @abstractmethod
    def default_config(self) -> Dict[str, Any]:
        """Return the default values for this bot's config slice.

        The dict's keys become the whitelist for ``apply_config_payload`` —
        anything not declared here is rejected. Use simple JSON-friendly
        types only (str / bool / int).
        """

    def read_config(self, cfg: "AssistantAIConfig") -> Dict[str, Any]:
        """Return this channel's config slice from ``cfg.bot_configs``,
        with defaults filling in any missing keys.
        """
        from .config_store import get_channel_config
        return get_channel_config(cfg, self.channel, self.default_config())

    def apply_config_payload(
        self, cfg: "AssistantAIConfig", payload: Dict[str, Any]
    ) -> None:
        """Merge ``payload`` into the channel slice of ``cfg.bot_configs``.

        ``payload`` is the dict the API caller supplied. Unknown keys are
        ignored; booleans / ints are coerced from JSON-y strings.
        """
        from .config_store import update_channel_config
        update_channel_config(cfg, self.channel, payload, self.default_config())

    @abstractmethod
    def is_enabled(self, cfg: "AssistantAIConfig") -> bool:
        """Return True iff this bot should run for the given AI config."""

    def has_default_recipient(self, cfg: "AssistantAIConfig") -> bool:
        """Whether this bot can deliver to a configured default receiver.

        Used to validate "forward web chat to bot": a forward with an empty
        recipient only reaches someone when the channel has a default target
        configured. Default ``False``; channels override to inspect their own
        addressing keys (e.g. Feishu ``default_receive_id`` / webhook, QQ
        ``default_target_id``).
        """
        return False

    # ---- long connection lifecycle -----------------------------------------

    @abstractmethod
    def start_long_connections(self) -> int:
        """(Re)start any long-running upstream client for this bot.

        Returns the number of configs that were brought up / refreshed.
        Implementations must be idempotent so the gateway / connector can
        call them on every keepalive tick.
        """

    @abstractmethod
    def get_long_connection_state(self, ai_config_id: int) -> Dict[str, str]:
        """Return ``{"status": "...", "label": "...", "message": "..."}``."""

    # ---- outbound messaging -------------------------------------------------
    #
    # Outbound delivery is orchestrated by ``bots.messaging.OutboundDispatcher``;
    # adapters contribute only these small primitives. ``parse_recipient`` turns
    # a loose caller-supplied addressing dict into the typed :class:`Recipient`
    # the channel understands (the one place per channel that knows its address
    # aliases); ``deliver_text`` / ``deliver_media`` perform a single wire send.

    @abstractmethod
    def parse_recipient(self, raw: Mapping[str, Any]) -> "Recipient":
        """Map a loose addressing dict onto this channel's :class:`Recipient`.

        An empty result (``Recipient(to_id="")``) signals "use the configured
        default receiver"; the adapter applies that fallback at delivery time.
        """

    @abstractmethod
    def deliver_text(
        self,
        *,
        user_id: int,
        ai_config_id: Optional[int],
        recipient: "Recipient",
        text: str,
    ) -> Any:
        """Send one plain-text message to ``recipient`` via this bot."""

    @abstractmethod
    def deliver_media(
        self,
        *,
        user_id: int,
        ai_config_id: Optional[int],
        recipient: "Recipient",
        media: "MediaPayload",
    ) -> Any:
        """Send one media message (with optional caption) to ``recipient``."""

    # ---- diagnostics ------------------------------------------------------

    def diagnose(self, cfg: "AssistantAIConfig", *, user_id: int) -> Dict[str, Any]:
        """Return a structured self-check for this bot's config.

        Subclasses override to validate credentials end-to-end (token
        fetch, endpoint reachability, addressing fields populated, …).
        The default returns ``{"supported": False}`` so unregistered or
        diagnostics-less bots don't crash the unified endpoint.

        Convention: include at least ``ok: bool`` so the UI can light a
        single status dot; richer detail goes into channel-specific keys
        the adapter knows about.
        """
        return {
            "ok": False,
            "supported": False,
            "message": f"diagnose not implemented for channel '{self.channel}'",
        }

    # ---- runtime tool requirements ----------------------------------------

    def extra_required_mcp_tools(self) -> Set[str]:
        """Extra MCP tools every chat that runs under this bot must allow.

        Default empty — most bots don't change the allowlist. Override per
        bot when the channel needs a tool that a stripped-down user config
        would otherwise omit (e.g. Feishu sessions always get
        ``conversation.edit``).
        """
        return set()



    # ---- formatting --------------------------------------------------------

    @abstractmethod
    def normalize_text(self, text: str, *, strip_markdown: bool = True) -> str:
        """Channel-specific text normalization for outbound messages."""

    # ---- notify dispatch ---------------------------------------------------

    @abstractmethod
    def load_session_route(
        self, session: "Session", message: "ChatMessage"
    ) -> Optional[Any]:
        """Look up the bot-specific routing row for an outbound message.

        Returns ``None`` when the message does not belong to this bot or
        no route is registered. Otherwise returns whatever bookkeeping
        object :meth:`notify_assistant_message` needs to deliver the reply.
        """

    @abstractmethod
    def notify_assistant_message(
        self,
        session: "Session",
        message: "ChatMessage",
        *,
        rendered_content: str,
        route: Any,
    ) -> None:
        """Deliver an already-rendered assistant message to the channel.

        ``rendered_content`` has already had MCP blocks stripped and any
        UI prefix (thinking / tool icons) applied. Adapters handle their
        own chunking, ack/sequence numbering, etc.
        """

    # ---- status -------------------------------------------------------------

    @abstractmethod
    def build_status(
        self,
        cfg: "AssistantAIConfig",
        *,
        remote_state: Optional[Dict[str, str]] = None,
        remote_error: Optional[str] = None,
    ) -> Dict[str, str]:
        """Return a UI status summary for the bots panel.

        ``remote_state`` is the state reported by connector-runtime for
        this config (when split deployment is in use). ``remote_error``
        is the connector-runtime error string when the remote call failed.
        When both are absent the adapter falls back to its in-process
        long-connection state.
        """


def channel_for_session_id(session_id: str, bots: Iterable[BotAdapter]) -> Optional[str]:
    """Identify which bot a ``ChatMessage.session_id`` belongs to via prefix.

    Returns the matching ``channel`` string or ``None`` if no bot claims it.
    """
    sid = str(session_id or "")
    for bot in bots:
        prefix = bot.session_prefix
        if prefix and sid.startswith(prefix):
            return bot.channel
    return None
