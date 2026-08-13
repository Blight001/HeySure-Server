"""Bot adapter registry — lookup by channel name + active-config iteration.

The registry is a thin process-local dict. Adapters register at import
time (see ``bots/__init__.py``); callers ask the registry for the right
adapter instead of branching on the channel string.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Dict, Iterator, List, Optional

from .base import BotAdapter

if TYPE_CHECKING:
    from api.models import AssistantAIConfig


_BOTS: Dict[str, BotAdapter] = {}
_DEFAULT_BOTS_LOADED = False


def _load_default_bots() -> None:
    """Import the built-in adapters once so they can self-register.

    Keeping the imports lazy avoids package-import side effects in processes
    that only need the registry helpers (for example the chat persistence
    notify hook).
    """
    global _DEFAULT_BOTS_LOADED
    if _DEFAULT_BOTS_LOADED:
        return
    _DEFAULT_BOTS_LOADED = True
    importlib.import_module("connector_runtime.bots.feishu.adapter")
    importlib.import_module("connector_runtime.bots.qq.adapter")
    importlib.import_module("connector_runtime.bots.wechat.adapter")


def register(bot: BotAdapter) -> None:
    """Register a bot adapter under its declared ``channel`` name.

    Re-registering the same channel replaces the prior adapter — useful
    for tests and hot-reload, intentionally lax.
    """
    channel = str(bot.channel or "").strip().lower()
    if not channel:
        raise ValueError("BotAdapter.channel must be a non-empty string")
    _BOTS[channel] = bot


def get(channel: str) -> Optional[BotAdapter]:
    """Return the adapter for ``channel`` or ``None`` if unknown."""
    _load_default_bots()
    return _BOTS.get(str(channel or "").strip().lower())


def require(channel: str) -> BotAdapter:
    """Return the adapter for ``channel`` or raise ``KeyError``."""
    bot = get(channel)
    if bot is None:
        raise KeyError(f"unknown bot channel: {channel!r}")
    return bot


def iter_bots() -> Iterator[BotAdapter]:
    """Yield every registered adapter (in registration order)."""
    _load_default_bots()
    return iter(_BOTS.values())


def all_channels() -> List[str]:
    """Return every registered channel name (whitelist for input validation)."""
    _load_default_bots()
    return list(_BOTS.keys())


def iter_active_for_config(cfg: "AssistantAIConfig") -> Iterator[BotAdapter]:
    """Yield only those adapters that are enabled for the given config.

    Every independently enabled channel is yielded. ``bot_channel`` is kept
    only as the preferred/default channel for backward compatibility.
    """
    _load_default_bots()
    for bot in _BOTS.values():
        if bot.is_enabled(cfg):
            yield bot


def sync_connection_directory(
    session,
    cfg: "AssistantAIConfig",
    *,
    preserve_existing: bool = False,
) -> None:
    """Persist the legacy config as each channel's default connection.

    Connector startup uses ``preserve_existing`` only to backfill deployments
    created before the connection directory existed.  This prevents a restart
    from overwriting instance settings edited through the connection API.
    """
    import time
    from sqlmodel import select
    from api.models import BotConnection
    from api.services.bot_directory import ensure_connection, project_channel_enabled, update_connection_config

    for bot in iter_bots():
        enabled = bool(bot.read_config(cfg).get("enabled"))
        row = session.exec(select(BotConnection).where(
            BotConnection.ai_config_id == int(cfg.id or 0),
            BotConnection.channel == bot.channel,
        ).order_by(BotConnection.is_default.desc(), BotConnection.created_at.asc())).first()
        created = enabled and row is None
        if created:
            row = ensure_connection(
                session, user_id=int(cfg.user_id), ai_config_id=int(cfg.id or 0),
                channel=bot.channel, name=bot.label,
            )
        if row is not None and (created or not preserve_existing):
            update_connection_config(row, bot.read_config(cfg), bot.default_config())
            row.is_default = True
            if bot.channel != "wechat":
                row.state = "configured" if enabled else "disabled"
            row.updated_at = time.time()
            session.add(row)
        if preserve_existing and row is not None:
            enabled_rows = session.exec(select(BotConnection).where(
                BotConnection.ai_config_id == int(cfg.id or 0),
                BotConnection.channel == bot.channel,
                BotConnection.state != "deleted",
            )).all()
            project_channel_enabled(cfg, bot.channel, any(item.enabled for item in enabled_rows))
            session.add(cfg)
    session.commit()


def backfill_legacy_connection_directories() -> None:
    """Create only missing connection rows for pre-directory AI configs."""
    from sqlmodel import Session, select
    from api.database import engine
    from api.models import AssistantAIConfig

    with Session(engine) as session:
        configs = session.exec(select(AssistantAIConfig)).all()
        for cfg in configs:
            sync_connection_directory(session, cfg, preserve_existing=True)
