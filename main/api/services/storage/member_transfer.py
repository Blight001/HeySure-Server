"""Streaming, capability-based transfer protocol for AI members and devices.

The MCP layer only exchanges this small envelope.  Bytes move through the
short-lived capability URL, so prompts and tool results never contain paths or
base64 payloads.  The same envelope is used in both directions:

* ``server_to_device``: ``GET`` downloads a registered workspace file.
* ``device_to_server``: ``PUT`` uploads bytes, then MCP ``commit`` registers
  the object in the member workspace and returns a normal ``file_ref``.

The transfer state is deliberately file-backed.  It avoids a schema migration
for a short-lived transport concern while remaining atomic across Gateway
workers and restart-safe.
"""

import hashlib
import json
import os
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Any, Dict

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

from api.core.settings import DATA_DIR
from api.services.storage.workspace_files import (
    MAX_WORKSPACE_FILE_BYTES,
    register_workspace_file,
    resolve_file_ref,
    save_workspace_stream,
)
from api.services.storage.workspace_scope import member_workspace_dir


PROTOCOL = "heysure.member-transfer"
PROTOCOL_VERSION = 1
DEFAULT_TTL_SECONDS = 300
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 900
TRANSFER_DIR = Path(DATA_DIR) / "member_transfers"
TRANSFER_ID_RE = re.compile(r"^xfer_[a-f0-9]{32}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{40,80}$")
_HASH_CHUNK_BYTES = 1024 * 1024
_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
}


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _transfer_path(transfer_id: str) -> Path:
    if not TRANSFER_ID_RE.fullmatch(str(transfer_id or "")):
        raise _error(404, "TRANSFER_NOT_FOUND", "transfer was not found")
    return TRANSFER_DIR / f"{transfer_id}.json"


def _write(record: Dict[str, Any]) -> None:
    TRANSFER_DIR.mkdir(parents=True, exist_ok=True)
    target = _transfer_path(str(record["transfer_id"]))
    temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(record, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _read(transfer_id: str) -> Dict[str, Any]:
    path = _transfer_path(transfer_id)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise _error(404, "TRANSFER_NOT_FOUND", "transfer was not found") from exc
    if not isinstance(record, dict) or record.get("transfer_id") != transfer_id:
        raise _error(404, "TRANSFER_NOT_FOUND", "transfer was not found")
    if float(record.get("expires_at") or 0) <= time.time():
        _remove_record(record)
        raise _error(410, "TRANSFER_EXPIRED", "transfer has expired")
    return record


def _remove_record(record: Dict[str, Any]) -> None:
    transfer_id = str(record.get("transfer_id") or "")
    if TRANSFER_ID_RE.fullmatch(transfer_id):
        _transfer_path(transfer_id).unlink(missing_ok=True)
    staging = str(record.get("staging_path") or "")
    if staging:
        try:
            Path(staging).unlink(missing_ok=True)
        except OSError:
            pass


def cleanup_expired_transfers(limit: int = 200) -> int:
    """Remove expired state and incomplete staging files without scanning user data."""
    if not TRANSFER_DIR.is_dir():
        return 0
    now = time.time()
    removed = 0
    for path in list(TRANSFER_DIR.glob("xfer_*.json"))[: max(1, min(int(limit), 1000))]:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            expired = float(record.get("expires_at") or 0) <= now
        except (OSError, ValueError, TypeError):
            expired = True
            record = {"transfer_id": path.stem}
        if expired:
            _remove_record(record)
            removed += 1
    return removed


def _bounded_ttl(value: Any) -> int:
    try:
        ttl = int(value or DEFAULT_TTL_SECONDS)
    except (TypeError, ValueError) as exc:
        raise _error(400, "INVALID_TTL", "ttl_seconds must be an integer") from exc
    if ttl < MIN_TTL_SECONDS or ttl > MAX_TTL_SECONDS:
        raise _error(400, "INVALID_TTL", "ttl_seconds must be between 60 and 900")
    return ttl


def _safe_name(value: Any) -> str:
    name = Path(str(value or "file.bin").strip()).name
    return name[:160] or "file.bin"


def _safe_folder(value: Any) -> str:
    folder = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value or "Imported")).strip("._")
    return folder[:80] or "Imported"


def _guess_mime(name: str) -> str:
    return _MIME_BY_SUFFIX.get(Path(name).suffix.lower(), "application/octet-stream")


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _token_matches(record: Dict[str, Any], token: str) -> bool:
    supplied = str(token or "")
    if not TOKEN_RE.fullmatch(supplied):
        return False
    return secrets.compare_digest(
        str(record.get("token_hash") or ""),
        hashlib.sha256(supplied.encode("utf-8")).hexdigest(),
    )


def _require_owner(record: Dict[str, Any], user_id: int, ai_config_id: int) -> None:
    if int(record.get("user_id") or 0) != int(user_id) or int(record.get("ai_config_id") or 0) != int(ai_config_id):
        raise _error(404, "TRANSFER_NOT_FOUND", "transfer was not found")


def _base_url(public_base_url: str) -> str:
    value = str(public_base_url or "").strip().rstrip("/")
    if not value.lower().startswith(("http://", "https://")):
        raise _error(503, "PUBLIC_BASE_URL_REQUIRED", "a public HTTP(S) base URL is required")
    return value


def _envelope(
    record: Dict[str, Any],
    base_url: str,
    *,
    token: str = "",
    include_secret: bool = True,
) -> Dict[str, Any]:
    direction = str(record["direction"])
    uploaded = record.get("state") in {"uploaded", "committed"}
    object_info = {
        "ref": record.get("file_ref"),
        "name": record.get("file_name"),
        "mime": record.get("mime_type") or "application/octet-stream",
        "bytes": record.get("bytes") if uploaded or direction == "server_to_device" else None,
        "sha256": record.get("sha256") if uploaded or direction == "server_to_device" else None,
    }
    object_info = {key: value for key, value in object_info.items() if value is not None}
    payload: Dict[str, Any] = {
        "protocol": PROTOCOL,
        "version": PROTOCOL_VERSION,
        "transfer_id": record["transfer_id"],
        "direction": direction,
        "state": record["state"],
        "object": object_info,
        "expires_at": record["expires_at"],
    }
    if direction == "server_to_device":
        payload["transport"] = {
            "method": "GET",
            "url": f"{base_url}/api/transfers/{record['transfer_id']}/{token}" if include_secret else None,
            "headers": {"Accept": record.get("mime_type") or "application/octet-stream"},
        }
    elif record["state"] == "prepared":
        payload["transport"] = {
            "method": "PUT",
            "url": f"{base_url}/api/transfers/{record['transfer_id']}/{token}" if include_secret else None,
            "headers": {"Content-Type": record.get("mime_type") or "application/octet-stream"},
        }
    if not include_secret:
        payload.pop("transport", None)
    return payload


def prepare_download(
    *,
    user_id: int,
    ai_config_id: int,
    file_ref: str = "",
    workspace_path: str = "",
    file_name: str = "",
    public_base_url: str,
    ttl_seconds: Any = DEFAULT_TTL_SECONDS,
) -> Dict[str, Any]:
    if not file_ref:
        registered = register_workspace_file(
            user_id=user_id,
            ai_config_id=ai_config_id,
            workspace_path=workspace_path,
            file_name=file_name,
        )
        file_ref = registered["file_ref"]
    record = resolve_file_ref(user_id=user_id, ai_config_id=ai_config_id, file_ref=file_ref, require_sendable=False)
    if int(record["bytes"]) > MAX_WORKSPACE_FILE_BYTES:
        raise _error(413, "FILE_TOO_LARGE", "file exceeds the 250 MB transfer limit")
    now = time.time()
    ttl = _bounded_ttl(ttl_seconds)
    transfer_id = f"xfer_{uuid.uuid4().hex}"
    token = secrets.token_urlsafe(32)
    stored = {
        "protocol": PROTOCOL,
        "version": PROTOCOL_VERSION,
        "transfer_id": transfer_id,
        "direction": "server_to_device",
        "state": "ready",
        "token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "user_id": int(user_id),
        "ai_config_id": int(ai_config_id),
        "file_ref": record["file_ref"],
        "file_name": record["file_name"],
        "mime_type": record["mime_type"] or _guess_mime(record["file_name"]),
        "bytes": int(record["bytes"]),
        "sha256": _sha256_file(record["server_path"]),
        "created_at": now,
        "expires_at": now + ttl,
    }
    cleanup_expired_transfers()
    _write(stored)
    return _envelope(stored, _base_url(public_base_url), token=token)


def prepare_upload(
    *,
    user_id: int,
    ai_config_id: int,
    file_name: str,
    mime_type: str = "",
    folder: str = "Imported",
    public_base_url: str,
    ttl_seconds: Any = DEFAULT_TTL_SECONDS,
) -> Dict[str, Any]:
    now = time.time()
    ttl = _bounded_ttl(ttl_seconds)
    transfer_id = f"xfer_{uuid.uuid4().hex}"
    token = secrets.token_urlsafe(32)
    safe_name = _safe_name(file_name)
    root = member_workspace_dir(user_id, ai_config_id, create=True)
    staging_dir = os.path.realpath(os.path.join(root, ".heysure", "transfers"))
    os.makedirs(staging_dir, exist_ok=True)
    staging_path = os.path.join(staging_dir, f"{transfer_id}.part")
    stored = {
        "protocol": PROTOCOL,
        "version": PROTOCOL_VERSION,
        "transfer_id": transfer_id,
        "direction": "device_to_server",
        "state": "prepared",
        "token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "user_id": int(user_id),
        "ai_config_id": int(ai_config_id),
        "file_name": safe_name,
        "mime_type": str(mime_type or _guess_mime(safe_name)).split(";", 1)[0],
        "folder": _safe_folder(folder),
        "staging_path": staging_path,
        "created_at": now,
        "expires_at": now + ttl,
    }
    cleanup_expired_transfers()
    _write(stored)
    return _envelope(stored, _base_url(public_base_url), token=token)


async def receive_upload(transfer_id: str, token: str, request: Request) -> Dict[str, Any]:
    record = _read(transfer_id)
    if not _token_matches(record, token):
        raise _error(404, "TRANSFER_NOT_FOUND", "transfer was not found")
    if record.get("direction") != "device_to_server" or record.get("state") != "prepared":
        raise _error(409, "TRANSFER_NOT_WRITABLE", "transfer is not ready for upload")
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > MAX_WORKSPACE_FILE_BYTES:
                raise _error(413, "FILE_TOO_LARGE", "file exceeds the 250 MB transfer limit")
        except ValueError:
            raise _error(400, "INVALID_CONTENT_LENGTH", "content-length must be an integer")
    path = str(record["staging_path"])
    size = 0
    digest = hashlib.sha256()
    try:
        with open(path, "xb") as handle:
            async for chunk in request.stream():
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_WORKSPACE_FILE_BYTES:
                    raise _error(413, "FILE_TOO_LARGE", "file exceeds the 250 MB transfer limit")
                digest.update(chunk)
                handle.write(chunk)
    except HTTPException:
        Path(path).unlink(missing_ok=True)
        raise
    except OSError as exc:
        Path(path).unlink(missing_ok=True)
        raise _error(500, "TRANSFER_WRITE_FAILED", "unable to store transfer bytes") from exc
    if size <= 0:
        Path(path).unlink(missing_ok=True)
        raise _error(400, "FILE_EMPTY", "file is empty")
    record.update({"state": "uploaded", "bytes": size, "sha256": digest.hexdigest()})
    _write(record)
    return {"protocol": PROTOCOL, "version": PROTOCOL_VERSION, "transfer_id": transfer_id, "state": "uploaded", "bytes": size, "sha256": record["sha256"]}


def commit_upload(*, user_id: int, ai_config_id: int, transfer_id: str, expected_bytes: Any = None, expected_sha256: str = "", public_base_url: str) -> Dict[str, Any]:
    record = _read(transfer_id)
    _require_owner(record, user_id, ai_config_id)
    if record.get("direction") != "device_to_server" or record.get("state") != "uploaded":
        raise _error(409, "TRANSFER_NOT_COMMITTABLE", "upload bytes before commit")
    if expected_bytes is not None and int(expected_bytes) != int(record["bytes"]):
        raise _error(409, "TRANSFER_CHECKSUM_MISMATCH", "uploaded byte count does not match")
    if expected_sha256 and not secrets.compare_digest(str(expected_sha256).lower(), str(record["sha256"]).lower()):
        raise _error(409, "TRANSFER_CHECKSUM_MISMATCH", "uploaded sha256 does not match")
    with open(str(record["staging_path"]), "rb") as handle:
        saved = save_workspace_stream(
            user_id=user_id,
            ai_config_id=ai_config_id,
            source=handle,
            file_name=str(record["file_name"]),
            folder=str(record["folder"]),
        )
    record.update({"state": "committed", "file_ref": saved["file_ref"], "workspace_path": saved["workspace_path"]})
    Path(str(record["staging_path"])).unlink(missing_ok=True)
    _write(record)
    result = _envelope(record, _base_url(public_base_url), include_secret=False)
    result["object"].update({"ref": saved["file_ref"], "name": saved["file_name"], "mime": saved["mime_type"], "bytes": saved["bytes"]})
    result["workspace_path"] = saved["workspace_path"]
    return result


def status_transfer(*, user_id: int, ai_config_id: int, transfer_id: str, public_base_url: str) -> Dict[str, Any]:
    record = _read(transfer_id)
    _require_owner(record, user_id, ai_config_id)
    return _envelope(record, _base_url(public_base_url), include_secret=False)


def abort_transfer(*, user_id: int, ai_config_id: int, transfer_id: str) -> Dict[str, Any]:
    record = _read(transfer_id)
    _require_owner(record, user_id, ai_config_id)
    if record.get("state") in {"committed", "aborted"}:
        raise _error(409, "TRANSFER_TERMINAL", "terminal transfers cannot be aborted")
    _remove_record(record)
    return {"protocol": PROTOCOL, "version": PROTOCOL_VERSION, "transfer_id": transfer_id, "state": "aborted"}


def download_transfer(transfer_id: str, token: str) -> FileResponse:
    record = _read(transfer_id)
    if record.get("direction") != "server_to_device" or not _token_matches(record, token):
        raise _error(404, "TRANSFER_NOT_FOUND", "transfer was not found")
    resolved = resolve_file_ref(
        user_id=int(record["user_id"]),
        ai_config_id=int(record["ai_config_id"]),
        file_ref=str(record["file_ref"]),
        require_sendable=False,
    )
    if int(resolved["bytes"]) != int(record["bytes"]) or _sha256_file(resolved["server_path"]) != record["sha256"]:
        raise _error(409, "TRANSFER_SOURCE_CHANGED", "source file changed after transfer preparation")
    return FileResponse(
        resolved["server_path"],
        media_type=record.get("mime_type") or "application/octet-stream",
        filename=record.get("file_name") or "download.bin",
        content_disposition_type="attachment",
        headers={
            "Cache-Control": "private, no-store",
            "X-HeySure-Transfer-Id": transfer_id,
            "X-HeySure-File-Sha256": str(record["sha256"]),
        },
    )
