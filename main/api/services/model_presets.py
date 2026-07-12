"""Model preset helpers: normalize, serialize, and resolve the per-user list of
``(model, api_key, base_url)`` presets used to configure AI inference."""

import json
from typing import Any, Optional

from api.models import AssistantAIConfig, User

# Sentinel base_url scheme carrying a local CLI command through the
# (api_key, base_url, model) tuple that resolve_model_preset returns, so the
# provider dispatch in chat_stream can recognize CLI presets without changing
# the tuple contract shared by all callers.
CLI_BASE_URL_PREFIX = "cli://"


def is_cli_base_url(base_url: Any) -> bool:
    return str(base_url or "").strip().lower().startswith(CLI_BASE_URL_PREFIX)


def cli_command_from_base_url(base_url: Any) -> str:
    value = str(base_url or "").strip()
    if value.lower().startswith(CLI_BASE_URL_PREFIX):
        return value[len(CLI_BASE_URL_PREFIX):].strip()
    return value


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
        provider = str(item.get("provider") or "api").strip().lower()
        if provider not in ("api", "cli"):
            provider = "api"
        cli_command = str(item.get("cli_command") or "").strip()
        if provider == "cli":
            if not model or not cli_command:
                continue
        elif not model or not api_key or not base_url:
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
                "provider": provider,
                "cli_command": cli_command,
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
                    "provider": "api",
                    "cli_command": "",
                }
            )
    return presets


def model_presets_json(raw: Any, user: Optional[User] = None) -> str:
    return json.dumps(normalize_model_presets(raw, user), ensure_ascii=False)


def resolve_model_preset(
    user: User,
    cfg: Optional[AssistantAIConfig] = None,
) -> tuple[str, str, str]:
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

    if selected is not None:
        if selected.get("provider") == "cli":
            # Carry the CLI command via the sentinel base_url; api_key is
            # unused for CLI presets but must stay non-empty for callers that
            # treat an empty key as "not configured".
            return (
                selected["api_key"] or "cli",
                CLI_BASE_URL_PREFIX + selected["cli_command"],
                selected["model"],
            )
        return selected["api_key"], selected["base_url"], selected["model"]

    if cfg is not None:
        return str(cfg.api_key or ""), str(cfg.base_url or ""), str(cfg.model or "")
    return str(user.admin_api_key or ""), str(user.admin_base_url or ""), str(user.admin_model or "")
