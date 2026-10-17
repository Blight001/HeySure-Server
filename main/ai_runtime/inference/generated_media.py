"""Persist media artifacts returned by Infinite Canvas generation tools."""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlmodel import Session

from ai_runtime.inference import tool_media


logger = logging.getLogger(__name__)
_MEDIA_PREFIXES = ("image/", "video/", "audio/")


def _safe_workspace_path(value: object) -> str:
    path = str(value or "").strip().replace("\\", "/")
    if not path or path.startswith("/") or ".." in path.split("/"):
        return ""
    return path


def _resolved_media_record(
    *,
    user_id: int,
    ai_config_id: Optional[int],
    item: dict[str, Any],
) -> Optional[dict[str, Any]]:
    from api.services.storage.workspace_files import resolve_file_ref

    file_ref = str(item.get("file_ref") or "").strip()
    try:
        resolved = resolve_file_ref(
            user_id=user_id,
            ai_config_id=ai_config_id,
            file_ref=file_ref,
            require_sendable=False,
        )
    except Exception:
        logger.warning("generated media workspace ref rejected file_ref=%s", file_ref[:80])
        return None
    path = _safe_workspace_path(resolved.get("workspace_path"))
    mime = str(resolved.get("mime_type") or "").strip().lower()
    if not path or not mime.startswith(_MEDIA_PREFIXES):
        logger.warning("generated media non-media workspace file rejected file_ref=%s", file_ref[:80])
        return None
    return {
        "file_ref": resolved.get("file_ref") or file_ref,
        "workspace_path": path,
        "file_name": resolved.get("file_name") or item.get("file_name") or "generated-media",
        "mime_type": mime,
        "bytes": resolved.get("bytes") or item.get("bytes") or 0,
    }


def _validated_records(
    *,
    user_id: int,
    ai_config_id: Optional[int],
    tool: str,
    tool_result: object,
) -> list[dict[str, Any]]:
    records = tool_media.generated_media_attachment_records(tool, tool_result)
    valid: list[dict[str, Any]] = []
    for item in records:
        record = _resolved_media_record(
            user_id=user_id,
            ai_config_id=ai_config_id,
            item=item,
        )
        if record:
            valid.append(record)
    return valid


def persist_generated_media_attachments(
    session: Session,
    *,
    message_id: int,
    user_id: int,
    ai_config_id: Optional[int],
    tool: str,
    tool_result: object,
) -> bool:
    """Bind validated uploaded artifacts to a completed MCP chat bubble."""

    try:
        records = _validated_records(
            user_id=user_id,
            ai_config_id=ai_config_id,
            tool=tool,
            tool_result=tool_result,
        )
        if not records:
            return False
        from api.services.chat.chat_attachments import bind_message_attachments

        rows = bind_message_attachments(
            session,
            message_id=message_id,
            user_id=user_id,
            ai_config_id=ai_config_id,
            records=records,
        )
        return bool(rows)
    except Exception:
        logger.warning("generated media attachment persist skipped", exc_info=True)
        return False
