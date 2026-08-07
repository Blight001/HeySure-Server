"""Encryption envelope for workflow runtime inputs.

Definitions never contain secrets. Runtime inputs are encrypted at rest as a
single authenticated Fernet envelope and are decrypted only while rendering a
step. Legacy plaintext JSON remains readable for forward migration.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from api.core.settings import settings


PREFIX = "fernet:v1:"


def _fernet() -> Fernet:
    material = (settings.workflow_encryption_secret or settings.jwt_secret).encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(material).digest())
    return Fernet(key)


def encrypt_json(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    return PREFIX + _fernet().encrypt(raw).decode("ascii")


def decrypt_json(value: str) -> Any:
    raw = str(value or "")
    if not raw.startswith(PREFIX):
        return json.loads(raw or "{}")
    try:
        clear = _fernet().decrypt(raw[len(PREFIX):].encode("ascii"))
    except (InvalidToken, ValueError) as exc:
        raise ValueError("workflow input cannot be decrypted with the configured key") from exc
    return json.loads(clear.decode("utf-8"))
