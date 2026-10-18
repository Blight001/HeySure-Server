"""Persist MCP-generated and referenced resources as chat attachments."""

from __future__ import annotations

import logging
import mimetypes
from urllib.parse import urljoin, urlparse
from typing import Any, Optional

import requests
from sqlmodel import Session

from api.core.settings import settings
from api.models import ChatMessage
from api.services.chat.chat_attachments import model_attachment_section
from api.services.storage.workspace_files import save_workspace_bytes
from ai_runtime.inference import tool_media


logger = logging.getLogger(__name__)
_MEDIA_PREFIXES = ("image/", "video/", "audio/")
_MAX_IMPORTED_RESOURCE_BYTES = 30 * 1024 * 1024
_CANVAS_IMAGE_PATH_PREFIX = "/api/gpt-image-2/images/"


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


def _resolved_resource_record(
    *,
    user_id: int,
    ai_config_id: Optional[int],
    item: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Resolve a connector-uploaded resource without restricting its MIME."""

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
        logger.warning("workspace resource ref rejected file_ref=%s", file_ref[:80])
        return None
    path = _safe_workspace_path(resolved.get("workspace_path"))
    mime = str(resolved.get("mime_type") or item.get("mime_type") or "application/octet-stream").strip().lower()
    if not path:
        return None
    return {
        "file_ref": resolved.get("file_ref") or file_ref,
        "workspace_path": path,
        "file_name": resolved.get("file_name") or item.get("file_name") or "resource",
        "mime_type": mime,
        "bytes": resolved.get("bytes") or item.get("bytes") or 0,
        "resource_kind": "file",
    }


def _source_url(value: object) -> str:
    """Resolve a prompt-library path against the configured canvas host."""

    raw = str(value or "").strip()
    base = str(settings.media_source_url or "").strip().rstrip("/")
    if not raw or not base:
        return ""
    parsed = urlparse(raw)
    base_parsed = urlparse(base)
    if parsed.scheme:
        # Absolute URLs are accepted only on the explicitly configured host;
        # prompt tool output must not become an SSRF primitive.
        if parsed.scheme not in {"http", "https"}:
            return ""
        if (parsed.scheme, parsed.netloc) != (base_parsed.scheme, base_parsed.netloc):
            return ""
        if not parsed.path.startswith(_CANVAS_IMAGE_PATH_PREFIX):
            return ""
        return raw
    if not raw.startswith("/"):
        return ""
    if not raw.startswith(_CANVAS_IMAGE_PATH_PREFIX):
        return ""
    return urljoin(f"{base}/", raw.lstrip("/"))


def _download_source(url: str) -> Optional[tuple[bytes, str]]:
    try:
        response = requests.get(url, timeout=(3, 15), stream=True, allow_redirects=False)
        response.raise_for_status()
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > _MAX_IMPORTED_RESOURCE_BYTES:
                logger.warning("canvas resource exceeds import limit url=%s", url[:120])
                return None
            chunks.append(chunk)
        data = b"".join(chunks)
        if not data:
            return None
        content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
        return data, content_type
    except (OSError, requests.RequestException, ValueError):
        logger.warning("canvas resource import skipped url=%s", url[:120], exc_info=True)
        return None


def _source_mime(item: dict[str, Any], content_type: str) -> str:
    if content_type.startswith("image/"):
        return content_type
    if content_type:
        return ""
    guessed = (
        str(item.get("mime_type") or "").lower()
        or mimetypes.guess_type(str(item.get("file_name") or ""))[0]
        or "image/jpeg"
    )
    return guessed if guessed.startswith("image/") else ""


def _import_source_record(
    *,
    user_id: int,
    ai_config_id: Optional[int],
    item: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Download a trusted canvas resource into the member workspace."""

    url = _source_url(item.get("source_url"))
    if not url:
        return None
    downloaded = _download_source(url)
    if not downloaded:
        return None
    data, content_type = downloaded
    mime_type = _source_mime(item, content_type)
    if not mime_type:
        return None
    file_name = str(item.get("file_name") or "prompt-reference.jpg").strip()
    record = save_workspace_bytes(
        user_id=user_id,
        ai_config_id=ai_config_id,
        data=data,
        file_name=file_name,
        folder="PromptReferences",
    )
    record["mime_type"] = mime_type
    record["resource_kind"] = "image"
    return record


def _prefer_uploaded_prompt_records(
    tool: str,
    uploaded: list[dict[str, Any]],
    generated: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not tool_media._is_canvas_prompt_tool(tool):
        return generated
    has_image = any(
        str(item.get("mime_type") or "").lower().startswith("image/")
        for item in uploaded
    )
    return [item for item in generated if not has_image or not item.get("source_url")]


def _unique_raw_records(
    uploaded: list[dict[str, Any]],
    generated: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [*uploaded, *generated]:
        identity = str(item.get("file_ref") or item.get("source_url") or "")
        if identity and identity in seen:
            continue
        seen.add(identity)
        records.append(item)
    return records


def _resolve_record(
    *,
    user_id: int,
    ai_config_id: Optional[int],
    item: dict[str, Any],
) -> Optional[dict[str, Any]]:
    if item.get("source_url"):
        return _import_source_record(
            user_id=user_id,
            ai_config_id=ai_config_id,
            item=item,
        )
    is_file = item.get("resource_kind") == "file"
    is_media = str(item.get("mime_type") or "").lower().startswith(_MEDIA_PREFIXES)
    resolver = _resolved_resource_record if is_file and not is_media else _resolved_media_record
    return resolver(user_id=user_id, ai_config_id=ai_config_id, item=item)


def _validated_records(
    *,
    user_id: int,
    ai_config_id: Optional[int],
    tool: str,
    tool_result: object,
) -> list[dict[str, Any]]:
    uploaded = tool_media.workspace_attachment_records(tool_result)
    generated = tool_media.generated_media_attachment_records(tool, tool_result)
    generated = _prefer_uploaded_prompt_records(tool, uploaded, generated)
    records = _unique_raw_records(uploaded, generated)
    valid: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in records:
        raw_identity = str(item.get("file_ref") or item.get("source_url") or "")
        if raw_identity and raw_identity in seen:
            continue
        record = _resolve_record(user_id=user_id, ai_config_id=ai_config_id, item=item)
        identity = str((record or {}).get("file_ref") or (record or {}).get("source_url") or "")
        if record and identity and identity not in seen:
            valid.append(record)
            seen.add(identity)
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
    """Bind validated MCP resources to a completed chat bubble."""

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
        if rows:
            _expose_records_to_current_tool_result(tool_result, records)
            _append_resource_manifest(session, message_id, rows)
        return bool(rows)
    except Exception:
        logger.warning("generated media attachment persist skipped", exc_info=True)
        return False


def _expose_records_to_current_tool_result(
    tool_result: object,
    records: list[dict[str, Any]],
) -> None:
    """Let the model use imported file refs in the same turn as the tool call."""

    if not isinstance(tool_result, dict):
        return
    payload = tool_result.get("result")
    target = payload if isinstance(payload, dict) else tool_result
    attachments = target.setdefault("heysure_attachments", [])
    if not isinstance(attachments, list):
        return
    seen = {
        str(item.get("file_ref") or "")
        for item in attachments
        if isinstance(item, dict)
    }
    for item in records:
        file_ref = str(item.get("file_ref") or "")
        if not file_ref or file_ref in seen:
            continue
        attachments.append({
            key: item.get(key)
            for key in ("file_ref", "workspace_path", "file_name", "mime_type", "bytes")
        })
        seen.add(file_ref)


def _append_resource_manifest(
    session: Session,
    message_id: int,
    rows: list[object],
) -> None:
    """Make imported resources available to the next model turn as text refs."""

    message = session.get(ChatMessage, int(message_id))
    if message is None or "[对话资源附件]" in str(message.content or ""):
        return
    records = [
        {
            "file_ref": getattr(row, "file_ref", ""),
            "workspace_path": getattr(row, "workspace_path", ""),
            "file_name": getattr(row, "file_name", ""),
            "mime_type": getattr(row, "mime_type", ""),
            "bytes": getattr(row, "bytes", 0),
        }
        for row in rows
    ]
    manifest = model_attachment_section(records).replace("[本轮上传附件]", "[对话资源附件]", 1)
    content = str(message.content or "")
    result_marker = "\n\n[结果]\n"
    if result_marker in content:
        content = content.replace(result_marker, f"{result_marker}{manifest}\n\n", 1)
    else:
        content = "\n\n".join(part for part in (content, manifest) if part)
    message.content = content
    session.add(message)
    session.commit()
