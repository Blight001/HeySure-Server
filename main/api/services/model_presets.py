"""Model preset helpers: normalize, serialize, and resolve the per-user list of
``(model, api_key, base_url)`` presets used to configure AI inference.

Besides the credential triple, a preset may carry two optional capability
fields so gateways that hide the real provider (e.g. a local CLI wrapped as an
OpenAI-compatible endpoint) can be configured explicitly instead of sniffed:

- ``provider``: ``auto`` (default, detect from base_url) / ``anthropic`` /
  ``openai`` — which wire protocol to speak.
- ``tool_protocol``: ``auto`` (default, native tools payload when MCP is on) /
  ``native`` / ``text`` — ``text`` skips the native ``tools`` payload entirely
  and relies on the prompt-taught ``<mcp-call>`` text protocol.
"""

import json
from typing import Any, Optional

from api.models import AssistantAIConfig, User

PRESET_PROVIDERS = ("auto", "anthropic", "openai")
PRESET_TOOL_PROTOCOLS = ("auto", "native", "text")


def _normalize_choice(value: Any, allowed: tuple[str, ...]) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else allowed[0]


def normalize_model_presets(raw: Any, user: Optional[User] = None) -> list[dict[str, str]]:
    try:
        parsed = json.loads(raw or "[]") if isinstance(raw, str) else raw
    except Exception:
        parsed = []
    if not isinstance(parsed, list):
        parsed = []

    presets: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(parsed):
        if not isinstance(item, dict):
            continue
        model = str(item.get("model") or "").strip()
        api_key = str(item.get("api_key") or "").strip()
        base_url = str(item.get("base_url") or "").strip()
        if not model or not api_key or not base_url:
            continue
        preset_id = str(item.get("id") or model or f"model_{index + 1}").strip()
        if not preset_id or preset_id in seen:
            preset_id = f"{model}_{index + 1}"
        seen.add(preset_id)
        presets.append(
            {
                "id": preset_id,
                "name": str(item.get("name") or model).strip() or model,
                "api_key": api_key,
                "base_url": base_url,
                "model": model,
                "provider": _normalize_choice(item.get("provider"), PRESET_PROVIDERS),
                "tool_protocol": _normalize_choice(item.get("tool_protocol"), PRESET_TOOL_PROTOCOLS),
            }
        )

    if not presets and user:
        model = str(getattr(user, "admin_model", "") or "").strip()
        api_key = str(getattr(user, "admin_api_key", "") or "").strip()
        base_url = str(getattr(user, "admin_base_url", "") or "").strip()
        if model and api_key and base_url:
            presets.append(
                {
                    "id": model,
                    "name": model,
                    "api_key": api_key,
                    "base_url": base_url,
                    "model": model,
                    "provider": "auto",
                    "tool_protocol": "auto",
                }
            )
    return presets


def model_presets_json(raw: Any, user: Optional[User] = None) -> str:
    return json.dumps(normalize_model_presets(raw, user), ensure_ascii=False)


def resolve_model_preset_entry(
    user: User,
    cfg: Optional[AssistantAIConfig] = None,
) -> Optional[dict[str, str]]:
    """Return the preset dict the given config resolves to, or None when the
    config falls through to its own literal (api_key, base_url, model) fields."""
    presets = normalize_model_presets(getattr(user, "model_presets", ""), user)
    preset_id = str(getattr(cfg, "model_preset_id", "") or "").strip() if cfg else ""
    model_name = str(getattr(cfg, "model", "") or "").strip() if cfg else str(getattr(user, "admin_model", "") or "").strip()

    selected = None
    if preset_id:
        selected = next((item for item in presets if item["id"] == preset_id), None)
    if selected is None and model_name:
        selected = next((item for item in presets if item["model"] == model_name or item["id"] == model_name), None)
    # Only auto-pick the first preset when the config did NOT pin any model.
    # If a model / preset was explicitly chosen but no longer matches a
    # preset (renamed, removed, typo), silently substituting presets[0]
    # would run inference on a DIFFERENT model than the one the user
    # selected and sees in the UI. Instead fall through to the config's own
    # literal fields below — which honors the chosen model when it carries
    # its own credentials, or surfaces a clear "not configured" error.
    if selected is None and presets and not preset_id and not model_name:
        selected = presets[0]
    return selected


def resolve_model_preset(
    user: User,
    cfg: Optional[AssistantAIConfig] = None,
) -> tuple[str, str, str]:
    selected = resolve_model_preset_entry(user, cfg)
    if selected is not None:
        return selected["api_key"], selected["base_url"], selected["model"]

    if cfg is not None:
        return str(cfg.api_key or ""), str(cfg.base_url or ""), str(cfg.model or "")
    return str(user.admin_api_key or ""), str(user.admin_base_url or ""), str(user.admin_model or "")
