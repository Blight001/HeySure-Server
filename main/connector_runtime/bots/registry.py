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

    AI configs currently pin to a single ``bot_channel`` at a time, so in
    practice this yields zero or one adapter. The iterator shape is kept
    for future support of multi-bot configs.
    """
    _load_default_bots()
    for bot in _BOTS.values():
        if bot.is_enabled(cfg):
            yield bot
