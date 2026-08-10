"""Member-scoped, opaque references for files that may be sent to users."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import HTTPException

from .workspace_scope import member_workspace_dir


MAX_SENDABLE_FILE_BYTES = 30 * 1024 * 1024
MAX_LIST_RESULTS = 100
FILE_REF_RE = re.compile(r"^file_[a-f0-9]{32}$")
_METADATA_DIR = os.path.join(".heysure", "file_refs")


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _root(user_id: int, ai_config_id: Optional[int]) -> str:
    return member_workspace_dir(user_id, ai_config_id, create=True)


def _inside(root: str, path: str, *, must_exist: bool = True) -> str:
    root_real = os.path.realpath(root)
    candidate = os.path.realpath(path)
    try:
        common = os.path.commonpath([root_real, candidate])
    except ValueError:
        common = ""
    if common != root_real:
        raise _error(403, "FILE_SCOPE_VIOLATION", "file must stay inside the current AI workspace")
    if must_exist and (not os.path.exists(candidate) or not os.path.isfile(candidate)):
        raise _error(404, "FILE_NOT_FOUND", "workspace file does not exist")
    return candidate


def resolve_workspace_path(
    user_id: int,
    ai_config_id: Optional[int],
    workspace_path: str,
) -> str:
    value = str(workspace_path or "").strip().replace("\\", "/")
    if not value:
        raise _error(400, "FILE_PATH_REQUIRED", "workspace_path is required")
    if os.path.isabs(value):
        raise _error(403, "ABSOLUTE_PATH_REJECTED", "use a path relative to the current AI workspace")
    root = _root(user_id, ai_config_id)
    return _inside(root, os.path.join(root, value))


def _metadata_path(root: str, file_ref: str) -> str:
    if not FILE_REF_RE.fullmatch(str(file_ref or "")):
        raise _error(400, "INVALID_FILE_REF", "file_ref is invalid")
    return os.path.join(root, _METADATA_DIR, f"{file_ref}.json")


def _public_record(root: str, metadata: Dict[str, Any], absolute_path: str) -> Dict[str, Any]:
    stat = os.stat(absolute_path)
    return {
        "file_ref": metadata["file_ref"],
        "workspace_path": metadata["workspace_path"],
        "file_name": metadata["file_name"],
        "mime_type": metadata["mime_type"],
        "bytes": stat.st_size,
        "created_at": metadata["created_at"],
        "can_send_to_user": stat.st_size <= MAX_SENDABLE_FILE_BYTES,
    }


def _validate_metadata(metadata: Any, file_ref: str) -> Dict[str, Any]:
    if not isinstance(metadata, dict) or metadata.get("file_ref") != file_ref:
        raise _error(400, "INVALID_FILE_REF", "file_ref metadata is invalid")
    required = ("workspace_path", "file_name", "mime_type", "created_at", "user_id")
    if any(key not in metadata for key in required):
        raise _error(400, "INVALID_FILE_REF", "file_ref metadata is incomplete")
    return metadata


def register_workspace_file(
    *,
    user_id: int,
    ai_config_id: Optional[int],
    workspace_path: str,
    file_name: str = "",
) -> Dict[str, Any]:
    root = _root(user_id, ai_config_id)
    absolute_path = resolve_workspace_path(user_id, ai_config_id, workspace_path)
    stat = os.stat(absolute_path)
    if stat.st_size <= 0:
        raise _error(400, "FILE_EMPTY", "file is empty")
    if stat.st_size > MAX_SENDABLE_FILE_BYTES:
        raise _error(400, "FILE_TOO_LARGE", "file exceeds the 30 MB send limit")
    relative = os.path.relpath(absolute_path, root).replace(os.sep, "/")
    safe_name = Path(str(file_name or "").strip()).name or Path(absolute_path).name
    mime_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    file_ref = f"file_{uuid.uuid4().hex}"
    metadata = {
        "version": 1,
        "file_ref": file_ref,
        "workspace_path": relative,
        "file_name": safe_name,
        "mime_type": mime_type,
        "created_at": time.time(),
        "user_id": int(user_id),
        "ai_config_id": int(ai_config_id) if ai_config_id else None,
    }
    metadata_path = _metadata_path(root, file_ref)
    os.makedirs(os.path.dirname(metadata_path), exist_ok=True)
    temp_path = f"{metadata_path}.{uuid.uuid4().hex}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False, separators=(",", ":"))
    os.replace(temp_path, metadata_path)
    return _public_record(root, metadata, absolute_path)


def save_workspace_bytes(
    *,
    user_id: int,
    ai_config_id: Optional[int],
    data: bytes,
    file_name: str,
    folder: str = "Imported",
) -> Dict[str, Any]:
    raw = bytes(data or b"")
    if not raw:
        raise _error(400, "FILE_EMPTY", "file is empty")
    if len(raw) > MAX_SENDABLE_FILE_BYTES:
        raise _error(400, "FILE_TOO_LARGE", "file exceeds the 30 MB send limit")
    safe_folder = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(folder or "Imported")).strip("._") or "Imported"
    safe_name = Path(str(file_name or "file.bin")).name
    stem = re.sub(r"[^a-zA-Z0-9_.-]+", "_", Path(safe_name).stem).strip("._") or "file"
    suffix = re.sub(r"[^a-zA-Z0-9.]", "", Path(safe_name).suffix)[:12]
    root = _root(user_id, ai_config_id)
    relative = f"{safe_folder}/{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}_{stem[:80]}{suffix}"
    absolute_path = _inside(root, os.path.join(root, relative), must_exist=False)
    os.makedirs(os.path.dirname(absolute_path), exist_ok=True)
    temp_path = f"{absolute_path}.{uuid.uuid4().hex}.tmp"
    with open(temp_path, "wb") as handle:
        handle.write(raw)
    os.replace(temp_path, absolute_path)
    return register_workspace_file(
        user_id=user_id,
        ai_config_id=ai_config_id,
        workspace_path=relative,
        file_name=safe_name,
    )


def resolve_file_ref(
    *,
    user_id: int,
    ai_config_id: Optional[int],
    file_ref: str,
) -> Dict[str, Any]:
    root = _root(user_id, ai_config_id)
    metadata_path = _metadata_path(root, str(file_ref or "").strip())
    if not os.path.isfile(metadata_path):
        raise _error(404, "FILE_NOT_FOUND", "file_ref was not found in the current AI workspace")
    try:
        with open(metadata_path, "r", encoding="utf-8") as handle:
            metadata = _validate_metadata(json.load(handle), str(file_ref or "").strip())
    except (OSError, ValueError, TypeError) as exc:
        raise _error(400, "INVALID_FILE_REF", "file_ref metadata is unreadable") from exc
    expected_ai = int(ai_config_id) if ai_config_id else None
    if metadata.get("user_id") != int(user_id) or metadata.get("ai_config_id") != expected_ai:
        raise _error(403, "FILE_SCOPE_VIOLATION", "file_ref belongs to another workspace")
    relative = str(metadata.get("workspace_path") or "")
    if os.path.isabs(relative):
        raise _error(403, "FILE_SCOPE_VIOLATION", "file_ref contains an unsafe path")
    absolute_path = _inside(root, os.path.join(root, relative))
    record = _public_record(root, metadata, absolute_path)
    if not record["can_send_to_user"]:
        raise _error(400, "FILE_TOO_LARGE", "file exceeds the 30 MB send limit")
    record["server_path"] = absolute_path
    return record


def list_file_refs(
    *,
    user_id: int,
    ai_config_id: Optional[int],
    limit: int = 50,
) -> Dict[str, Any]:
    root = _root(user_id, ai_config_id)
    metadata_dir = os.path.join(root, _METADATA_DIR)
    bounded = max(1, min(int(limit or 50), MAX_LIST_RESULTS))
    records = []
    for path in sorted(Path(metadata_dir).glob("file_*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if os.path.isdir(metadata_dir) else []:
        try:
            record = resolve_file_ref(
                user_id=user_id,
                ai_config_id=ai_config_id,
                file_ref=path.stem,
            )
            record.pop("server_path", None)
            records.append(record)
        except HTTPException:
            continue
        if len(records) >= bounded:
            break
    return {"files": records, "count": len(records)}


def unregister_file_ref(
    *,
    user_id: int,
    ai_config_id: Optional[int],
    file_ref: str,
) -> Dict[str, Any]:
    root = _root(user_id, ai_config_id)
    metadata_path = _metadata_path(root, str(file_ref or "").strip())
    resolve_file_ref(user_id=user_id, ai_config_id=ai_config_id, file_ref=file_ref)
    os.remove(metadata_path)
    return {"removed": True, "file_ref": file_ref, "file_deleted": False}
