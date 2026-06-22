"""Default config schema + read helper for the Feishu bot.

Lives in its own module so service / long_connection / router / adapter
all reference one source of truth without circular imports.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict

from ..config_store import get_channel_config

if TYPE_CHECKING:
    from api.models import AssistantAIConfig


CHANNEL = "feishu"

# Every key listed here is part of the Feishu adapter's public config schema.
# Adding a new field: add it here + in any router/services that need to read
# it. Frontend sends the matching key under ``bot_configs.feishu.<key>``.
FEISHU_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "webhook_url": "",
    "app_id": "",
    "app_secret": "",
    "verification_token": "",
    "default_receive_id": "",
    "default_receive_id_type": "chat_id",
}


def read_feishu_config(cfg: "AssistantAIConfig") -> Dict[str, Any]:
    """Return Feishu's slice of ``cfg.bot_configs`` with defaults applied."""
    return get_channel_config(cfg, CHANNEL, FEISHU_DEFAULTS)
