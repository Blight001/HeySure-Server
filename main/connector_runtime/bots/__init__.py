"""Bot plugin system — one self-contained package per chat channel.

Every supported bot (Feishu, QQ, …) lives under ``bots/<name>/`` and is
exposed through a uniform :class:`BotAdapter` interface. Code outside this
package never branches on ``"feishu"`` / ``"qq"`` directly — it asks the
registry for the right adapter and invokes the methods it needs.

Adding a new bot:
    1. Create ``bots/<name>/`` with an ``adapter.py`` that subclasses
       :class:`BotAdapter` and instantiates the adapter at import time.
    2. Import that module from ``connector_runtime.bots.registry`` so the
       adapter registers when the registry is first used.
    3. Add the per-bot config columns / DB models in ``bots/<name>/models.py``
       and run a migration to add them to ``assistantaiconfig``.
"""

from .base import BotAdapter
from .registry import all_channels, get, iter_active_for_config, iter_bots, register


__all__ = [
    "BotAdapter",
    "all_channels",
    "get",
    "iter_active_for_config",
    "iter_bots",
    "register",
]
