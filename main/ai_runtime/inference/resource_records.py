"""Normalize workspace resources returned by MCP tools.

This module keeps the media extraction rules separate from the image-context
helpers in :mod:`tool_media`.  The public functions are intentionally small
and MIME-agnostic so documents and media use the same attachment contract.
"""

from __future__ import annotations

import mimetypes
import os
from urllib.parse import unquote, urlparse
from typing import Any, Iterator, Optional


_CANVAS_GENERATION_TOOLS = {
    "canvas_image_generate",
    "canvas_video_generate",
    "canvas_generation_image",
    "canvas_generation_video",
}
_CANVAS_PROMPT_TOOLS = {"canvas_prompt_list", "canvas_prompt_get"}


def is_canvas_generation_tool(tool: str) -> bool:
    normalized = str(tool or "").strip().lower().replace("+", ".")
    return any(
        normalized == name or normalized.endswith(f".{name}")
        for name in _CANVAS_GENERATION_TOOLS
    )


def is_canvas_prompt_tool(tool: str) -> bool:
    normalized = str(tool or "").strip().lower().replace("+", ".")
    return any(
        normalized == name or normalized.endswith(f".{name}")
        for name in _CANVAS_PROMPT_TOOLS
    )


def _walk_payload(value: object, depth: int = 0) -> Iterator[dict[str, Any]]:
    if depth > 7:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_payload(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_payload(child, depth + 1)


def _safe_path(value: object) -> str:
    path = str(value or "").strip().replace("\\", "/")
    if not path or path.startswith("/") or ".." in path.split("/"):
        return ""
    return path


def _record_identity(record: Optional[dict[str, Any]]) -> str:
    return str((record or {}).get("source_url") or (record or {}).get("file_ref") or "")


def _size(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _workspace_record(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    path = _safe_path(item.get("workspace_path"))
    file_ref = str(item.get("file_ref") or "").strip()
    mime_type = str(item.get("mime_type") or "image/jpeg").strip().lower()
    if not file_ref or not path or not mime_type.startswith("image/"):
        return None
    return {
        "file_ref": file_ref,
        "workspace_path": path,
        "file_name": str(item.get("file_name") or os.path.basename(path) or "prompt-reference"),
        "mime_type": mime_type,
        "bytes": _size(item.get("bytes")),
        "resource_kind": "file",
    }


def _source_fields(item: object) -> tuple[str, str, str]:
    if isinstance(item, dict):
        value = item.get("url") or item.get("image_url") or item.get("reference_image")
        name = str(item.get("file_name") or "").strip()
        mime = str(item.get("mime_type") or "").strip().lower()
    else:
        value, name, mime = item, "", ""
    return str(value or "").strip(), name, mime


def _source_record(item: object) -> Optional[dict[str, Any]]:
    source_url, file_name, mime_type = _source_fields(item)
    if not source_url or source_url.startswith("data:"):
        return None
    parsed = urlparse(source_url)
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        return None
    path_name = unquote(parsed.path.rsplit("/", 1)[-1]) if parsed.path else ""
    file_name = os.path.basename(file_name or path_name or "prompt-reference") or "prompt-reference"
    mime_type = mime_type or mimetypes.guess_type(file_name)[0] or "image/jpeg"
    if not mime_type.startswith("image/"):
        return None
    return {
        "source_url": source_url,
        "file_name": file_name,
        "mime_type": mime_type,
        "bytes": 0,
        "resource_kind": "image",
    }


def prompt_resource_record(item: object) -> Optional[dict[str, Any]]:
    """Normalize one prompt-library reference image."""

    if isinstance(item, dict) and item.get("file_ref") and item.get("workspace_path"):
        return _workspace_record(item)
    return _source_record(item)


def _valid_generated_record(item: object) -> Optional[dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    path = _safe_path(item.get("workspace_path"))
    file_ref = str(item.get("file_ref") or "").strip()
    mime_type = str(item.get("mime_type") or "").strip().lower()
    if not file_ref or not path or not mime_type.startswith(("image/", "video/", "audio/")):
        return None
    return {
        "file_ref": file_ref,
        "workspace_path": path,
        "file_name": str(item.get("file_name") or os.path.basename(path) or "generated-media").strip(),
        "mime_type": mime_type,
        "bytes": _size(item.get("bytes")),
    }


def _extract_candidates(mapping: dict[str, Any], keys: tuple[str, ...]) -> list[object]:
    result: list[object] = []
    for key in keys:
        values = mapping.get(key)
        if isinstance(values, list):
            result.extend(values)
        elif key == "reference_image" and values is not None:
            result.append(values)
    return result


def _append_unique(
    records: list[dict[str, Any]],
    seen: set[str],
    record: Optional[dict[str, Any]],
) -> bool:
    identity = _record_identity(record)
    if not record or not identity or identity in seen:
        return False
    records.append(record)
    seen.add(identity)
    return True


def prompt_reference_attachment_records(
    tool_result: object,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    max_records = max(1, int(limit))
    for mapping in _walk_payload(tool_result):
        for candidate in _extract_candidates(mapping, ("reference_image", "reference_images")):
            _append_unique(records, seen, prompt_resource_record(candidate))
            if len(records) >= max_records:
                return records
    return records


def _records_from_uploaded(
    mapping: dict[str, Any],
    seen: set[str],
    records: list[dict[str, Any]],
    limit: int,
) -> bool:
    for key in ("uploaded_files", "heysure_attachments"):
        uploaded = mapping.get(key)
        if not isinstance(uploaded, list):
            continue
        for item in uploaded:
            record = _valid_generated_record(item)
            if _append_unique(records, seen, record) and len(records) >= limit:
                return True
    return False


def generated_media_attachment_records(
    tool: str,
    tool_result: object,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    if is_canvas_prompt_tool(tool):
        return prompt_reference_attachment_records(tool_result, limit=limit)
    if not is_canvas_generation_tool(tool):
        return []
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    max_records = max(1, int(limit))
    for mapping in _walk_payload(tool_result):
        if _records_from_uploaded(mapping, seen, records, max_records):
            return records
    return records


def _workspace_resource(item: object) -> Optional[dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    path = _safe_path(item.get("workspace_path"))
    file_ref = str(item.get("file_ref") or "").strip()
    if not file_ref or not path:
        return None
    return {
        "file_ref": file_ref,
        "workspace_path": path,
        "file_name": str(item.get("file_name") or os.path.basename(path) or "resource"),
        "mime_type": str(item.get("mime_type") or "application/octet-stream").strip().lower(),
        "bytes": _size(item.get("bytes")),
        "resource_kind": "file",
    }


def workspace_attachment_records(
    tool_result: object,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    max_records = max(1, int(limit))
    for mapping in _walk_payload(tool_result):
        for key in ("uploaded_files", "heysure_attachments"):
            uploaded = mapping.get(key)
            if not isinstance(uploaded, list):
                continue
            for item in uploaded:
                record = _workspace_resource(item)
                if _append_unique(records, seen, record) and len(records) >= max_records:
                    return records
    return records

