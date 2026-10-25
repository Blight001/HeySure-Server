"""MCP entry point for the canonical bidirectional member-transfer protocol."""

from typing import Any, Dict, Optional

from fastapi import HTTPException

from api.services.storage.member_transfer import (
    abort_transfer,
    commit_upload,
    prepare_download,
    prepare_upload,
    status_transfer,
)
from api.services.storage.temporary_file_links import configured_public_base_url


WORKSPACE_TRANSFER_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["prepare_download", "prepare_upload", "commit", "status", "abort"],
            "description": "Canonical byte-transfer lifecycle. MCP exchanges references; bytes move through the returned streaming URL.",
        },
        "transfer_id": {"type": "string", "description": "Opaque xfer_... returned by prepare_*.",},
        "file_ref": {"type": "string", "description": "Server workspace file_ref for prepare_download.",},
        "workspace_path": {"type": "string", "description": "Relative AI workspace path for prepare_download when file_ref is not known.",},
        "file_name": {"type": "string", "description": "Name used for the transferred object.",},
        "mime_type": {"type": "string", "description": "Optional media MIME type for prepare_upload.",},
        "folder": {"type": "string", "description": "Workspace folder used when committing an upload.",},
        "ttl_seconds": {"type": "integer", "minimum": 60, "maximum": 900, "description": "Capability lifetime; default 300 seconds.",},
        "bytes": {"type": "integer", "minimum": 1, "description": "Expected uploaded byte count for commit.",},
        "sha256": {"type": "string", "description": "Expected uploaded SHA-256 for commit.",},
    },
    "required": ["action"],
}


def _require_ai_config(ai_config_id: Optional[int]) -> int:
    if not ai_config_id:
        raise HTTPException(status_code=400, detail="ai_config_id is required for member transfers")
    return int(ai_config_id)


def _workspace_transfer(
    user_id: int,
    args: Dict[str, Any],
    ai_config_id: Optional[int],
) -> Dict[str, Any]:
    config_id = _require_ai_config(ai_config_id)
    action = str((args or {}).get("action") or "").strip().lower()
    base_url = configured_public_base_url()
    if action == "prepare_download":
        return prepare_download(
            user_id=user_id,
            ai_config_id=config_id,
            file_ref=str(args.get("file_ref") or "").strip(),
            workspace_path=str(args.get("workspace_path") or "").strip(),
            file_name=str(args.get("file_name") or "").strip(),
            public_base_url=base_url,
            ttl_seconds=args.get("ttl_seconds"),
        )
    if action == "prepare_upload":
        file_name = str(args.get("file_name") or "").strip()
        if not file_name:
            raise HTTPException(status_code=400, detail="file_name is required for prepare_upload")
        return prepare_upload(
            user_id=user_id,
            ai_config_id=config_id,
            file_name=file_name,
            mime_type=str(args.get("mime_type") or ""),
            folder=str(args.get("folder") or "Imported"),
            public_base_url=base_url,
            ttl_seconds=args.get("ttl_seconds"),
        )
    transfer_id = str(args.get("transfer_id") or "").strip()
    if not transfer_id:
        raise HTTPException(status_code=400, detail="transfer_id is required")
    if action == "commit":
        return commit_upload(
            user_id=user_id,
            ai_config_id=config_id,
            transfer_id=transfer_id,
            expected_bytes=args.get("bytes"),
            expected_sha256=str(args.get("sha256") or "").strip(),
            public_base_url=base_url,
        )
    if action == "status":
        return status_transfer(
            user_id=user_id,
            ai_config_id=config_id,
            transfer_id=transfer_id,
            public_base_url=base_url,
        )
    if action == "abort":
        return abort_transfer(user_id=user_id, ai_config_id=config_id, transfer_id=transfer_id)
    raise HTTPException(status_code=400, detail="unsupported workspace.transfer action")
