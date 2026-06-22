"""Read/write helpers for the ``AssistantAIConfig.bot_configs`` JSON column.

The column stores ``{<channel>: {<key>: <value>, ...}, ...}``. Each adapter
declares its own ``default_config()`` schema and uses these helpers to
extract / mutate its slice without other bots' code knowing about it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Dict

from api.value_utils import to_bool

if TYPE_CHECKING:
    from api.models import AssistantAIConfig


def load_bot_configs(cfg: "AssistantAIConfig") -> Dict[str, Dict[str, Any]]:
    """Decode ``cfg.bot_configs`` to a dict-of-dicts; ``{}`` on parse failure."""
    raw = str(getattr(cfg, "bot_configs", "") or "").strip() or "{}"
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for channel, payload in data.items():
        if isinstance(payload, dict):
            out[str(channel)] = dict(payload)
    return out


def save_bot_configs(cfg: "AssistantAIConfig", configs: Dict[str, Dict[str, Any]]) -> None:
    """Serialize ``configs`` back onto ``cfg.bot_configs``."""
    cfg.bot_configs = json.dumps(configs, ensure_ascii=False)


def get_channel_config(
    cfg: "AssistantAIConfig",
    channel: str,
    defaults: Dict[str, Any],
) -> Dict[str, Any]:
    """Return the channel's slice merged on top of ``defaults``.

    Missing keys fall back to the defaults so callers can rely on every
    key being present without writing per-field ``or ""`` guards.
    """
    payload = load_bot_configs(cfg).get(str(channel), {})
    merged: Dict[str, Any] = dict(defaults)
    for key, value in payload.items():
        # Only honor keys the adapter declared in its defaults — keeps
        # accidental garbage in the JSON from leaking through.
        if key in merged:
            merged[key] = value
    return merged


def update_channel_config(
    cfg: "AssistantAIConfig",
    channel: str,
    payload: Dict[str, Any],
    defaults: Dict[str, Any],
) -> None:
    """Merge ``payload`` into the channel slice, gated by ``defaults`` keys.

    The merge is shallow + key-filtered: only declared keys are written;
    everything else in ``payload`` is silently ignored. Type-coerce booleans
    so JSON strings like ``"true"`` don't sneak in.
    """
    configs = load_bot_configs(cfg)
    current = dict(defaults)
    current.update(configs.get(channel, {}))
    for key, default_value in defaults.items():
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(default_value, bool):
            current[key] = to_bool(value, default_value)
        elif isinstance(default_value, int) and not isinstance(default_value, bool):
            try:
                current[key] = int(value)
            except (TypeError, ValueError):
                current[key] = default_value
        else:
            current[key] = "" if value is None else str(value)
    configs[channel] = current
    save_bot_configs(cfg, configs)
