"""Short-lived, opaque capability links for member workspace files."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Any, Dict

from fastapi import HTTPException

from api.core.settings import DATA_DIR, settings
from .workspace_files import resolve_file_ref


DEFAULT_TTL_SECONDS = 300
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 900
GRANT_ID_RE = re.compile(r"^fgrant_[a-f0-9]{32}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{40,80}$")
GRANT_DIR = Path(DATA_DIR) / "temp_file_grants"
_HASH_CHUNK_BYTES = 1024 * 1024


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _grant_path(grant_id: str) -> Path:
    if not GRANT_ID_RE.fullmatch(str(grant_id or "")):
        raise _error(404, "TEMP_LINK_NOT_FOUND", "temporary file link was not found")
    return GRANT_DIR / f"{grant_id}.json"


def _write_grant(record: Dict[str, Any]) -> None:
    GRANT_DIR.mkdir(parents=True, exist_ok=True)
    target = _grant_path(str(record["grant_id"]))
    temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _read_grant(grant_id: str) -> Dict[str, Any]:
    path = _grant_path(grant_id)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise _error(404, "TEMP_LINK_NOT_FOUND", "temporary file link was not found") from exc
    if not isinstance(record, dict) or record.get("grant_id") != grant_id:
        raise _error(404, "TEMP_LINK_NOT_FOUND", "temporary file link was not found")
    return record


def _normalize_base_url(value: str) -> str:
    base = str(value or "").strip().rstrip("/")
    if not base.lower().startswith(("http://", "https://")):
        raise _error(
            503,
            "PUBLIC_BASE_URL_REQUIRED",
            "PUBLIC_BASE_URL or AGENT_SOCKET_URL must be configured before creating temporary links",
        )
    return base


def configured_public_base_url() -> str:
    return _normalize_base_url(settings.public_base_url or settings.agent_socket_url)


def _bounded_ttl(value: Any) -> int:
    try:
        ttl = int(value or DEFAULT_TTL_SECONDS)
    except (TypeError, ValueError) as exc:
        raise _error(400, "INVALID_TTL", "ttl_seconds must be an integer") from exc
    if ttl < MIN_TTL_SECONDS or ttl > MAX_TTL_SECONDS:
        raise _error(400, "INVALID_TTL", "ttl_seconds must be between 60 and 900")
    return ttl


def cleanup_expired_grants(now: float | None = None, limit: int = 100) -> int:
    current = float(now if now is not None else time.time())
    if not GRANT_DIR.is_dir():
        return 0
    removed = 0
    for path in list(GRANT_DIR.glob("fgrant_*.json"))[: max(1, min(limit, 1000))]:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if float(record.get("expires_at") or 0) > current:
                continue
        except (OSError, ValueError, TypeError):
            pass
        path.unlink(missing_ok=True)
        removed += 1
    return removed


def create_temporary_file_link(
    *,
    user_id: int,
    ai_config_id: int,
    file_ref: str,
    public_base_url: str,
    ttl_seconds: Any = DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> Dict[str, Any]:
    current = float(now if now is not None else time.time())
    ttl = _bounded_ttl(ttl_seconds)
    record = resolve_file_ref(
        user_id=int(user_id),
        ai_config_id=int(ai_config_id),
        file_ref=str(file_ref or "").strip(),
    )
    grant_id = f"fgrant_{uuid.uuid4().hex}"
    token = secrets.token_urlsafe(32)
    expires_at = current + ttl
    stored = {
        "version": 1,
        "grant_id": grant_id,
        "token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "user_id": int(user_id),
        "ai_config_id": int(ai_config_id),
        "file_ref": record["file_ref"],
        "file_name": record["file_name"],
        "mime_type": record["mime_type"],
        "bytes": int(record["bytes"]),
        "sha256": _sha256_file(record["server_path"]),
        "created_at": current,
        "expires_at": expires_at,
    }
    cleanup_expired_grants(current)
    _write_grant(stored)
    base = _normalize_base_url(public_base_url)
    return {
        "grant_id": grant_id,
        "url": f"{base}/api/tmp-files/{grant_id}/{token}",
        "file_ref": stored["file_ref"],
        "file_name": stored["file_name"],
        "mime_type": stored["mime_type"],
        "bytes": stored["bytes"],
        "sha256": stored["sha256"],
        "expires_at": expires_at,
        "ttl_seconds": ttl,
    }


def resolve_temporary_file_link(
    grant_id: str,
    token: str,
    *,
    now: float | None = None,
) -> Dict[str, Any]:
    record = _read_grant(grant_id)
    supplied = str(token or "")
    valid_token = TOKEN_RE.fullmatch(supplied) and secrets.compare_digest(
        str(record.get("token_hash") or ""),
        hashlib.sha256(supplied.encode("utf-8")).hexdigest(),
    )
    current = float(now if now is not None else time.time())
    if not valid_token or float(record.get("expires_at") or 0) <= current:
        if current >= float(record.get("expires_at") or 0):
            _grant_path(grant_id).unlink(missing_ok=True)
        raise _error(404, "TEMP_LINK_NOT_FOUND", "temporary file link was not found")
    resolved = resolve_file_ref(
        user_id=int(record["user_id"]),
        ai_config_id=int(record["ai_config_id"]),
        file_ref=str(record["file_ref"]),
    )
    if int(resolved["bytes"]) != int(record["bytes"]) or _sha256_file(resolved["server_path"]) != record["sha256"]:
        raise _error(409, "TEMP_LINK_SOURCE_CHANGED", "the source file changed after this link was created")
    return {**record, "server_path": resolved["server_path"]}


def revoke_temporary_file_link(*, user_id: int, ai_config_id: int, grant_id: str) -> Dict[str, Any]:
    record = _read_grant(grant_id)
    if int(record.get("user_id") or 0) != int(user_id) or int(record.get("ai_config_id") or 0) != int(ai_config_id):
        raise _error(404, "TEMP_LINK_NOT_FOUND", "temporary file link was not found")
    _grant_path(grant_id).unlink(missing_ok=True)
    return {"revoked": True, "grant_id": grant_id}
