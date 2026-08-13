"""Authenticated encryption for long-lived bot connection credentials."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Dict

from cryptography.fernet import Fernet, InvalidToken

from api.core.settings import settings


PREFIX = "fernet:v1:"


def _fernet() -> Fernet:
    material = (settings.bot_encryption_secret or settings.jwt_secret).encode("utf-8")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(material).digest()))


def encrypt_credentials(value: Dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return PREFIX + _fernet().encrypt(raw).decode("ascii")


def decrypt_credentials(value: str) -> Dict[str, Any]:
    raw = str(value or "")
    if not raw:
        return {}
    try:
        clear = _fernet().decrypt(raw.removeprefix(PREFIX).encode("ascii"))
    except (InvalidToken, ValueError) as exc:
        raise ValueError("bot credentials cannot be decrypted with the configured key") from exc
    parsed = json.loads(clear.decode("utf-8"))
    return parsed if isinstance(parsed, dict) else {}
