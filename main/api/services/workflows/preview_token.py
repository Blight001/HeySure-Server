"""Short-lived encrypted previews for committing validated definition changes."""
from __future__ import annotations

import time
from typing import Any, Dict

from .compiler import WorkflowValidationError
from .secrets import decrypt_json, encrypt_json

PREVIEW_TOKEN_TTL_SECONDS = 15 * 60


def issue_preview_token(*, action: str, user_id: int, card_id: str, base_version_id: str, payload: Any) -> str:
    return encrypt_json({
        "kind": "workflow_change_preview",
        "action": action,
        "user_id": user_id,
        "card_id": card_id,
        "base_version_id": base_version_id,
        "payload": payload,
        "expires_at": time.time() + PREVIEW_TOKEN_TTL_SECONDS,
    })


def consume_preview_token(
    token: str, *, action: str, user_id: int, card_id: str, base_version_id: str,
) -> Any:
    try:
        value: Dict[str, Any] = decrypt_json(token)
    except Exception as exc:
        raise WorkflowValidationError(["preview_token is invalid or cannot be decrypted"]) from exc
    expected = {
        "kind": "workflow_change_preview", "action": action, "user_id": user_id,
        "card_id": card_id, "base_version_id": base_version_id,
    }
    if not isinstance(value, dict) or any(value.get(key) != wanted for key, wanted in expected.items()):
        raise WorkflowValidationError(["preview_token does not match this action, user, card, or base_version_id"])
    if float(value.get("expires_at") or 0) < time.time():
        raise WorkflowValidationError(["preview_token expired; run dry_run again"])
    return value.get("payload")
