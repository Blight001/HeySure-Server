"""Default config schema + read helper for the QQ bot."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict

from ..config_store import get_channel_config

if TYPE_CHECKING:
    from api.models import AssistantAIConfig


CHANNEL = "qq"

QQ_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "app_id": "",
    "app_secret": "",
    "sandbox": False,
    "default_target_id": "",
    "default_target_type": "c2c",
    # Rich-output options. Native markdown + streaming are QQ "灰度/whitelist"
    # capabilities — both default ON here but every send auto-falls back to
    # plain text (msg_type=0) when the open platform rejects the request, so a
    # bot that has not been granted the feature still delivers messages.
    "markdown_mode": "native",       # native | template | off
    "markdown_template_id": "",      # required only when markdown_mode == "template"
    "stream_enabled": True,          # typewriter-style streaming of the live answer
}


def read_qq_config(cfg: "AssistantAIConfig") -> Dict[str, Any]:
    """Return QQ's slice of ``cfg.bot_configs`` with defaults applied."""
    return get_channel_config(cfg, CHANNEL, QQ_DEFAULTS)
