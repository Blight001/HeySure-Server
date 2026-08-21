"""Shared line editing and CRUD adapters for file-backed knowledge entries."""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List

from .librarian_core import (
    _entry_dict_from_file_entry,
    _load_user_knowledge_entries,
    _normalize_endpoint,
    _normalize_triggers,
    _upsert_thought,
)
from .librarian_topics import delete_topic, update_topic_content


def _text_sha256(content: str) -> str:
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


def _line_number(value: Any, field: str, line_count: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if number < 1 or number > line_count:
        raise ValueError(f"{field} must be between 1 and {line_count}")
    return number


def _edit_text(edit: Dict[str, Any]) -> str:
    if "text" in edit:
        return str(edit.get("text") or "")
    if "content" in edit:
        return str(edit.get("content") or "")
    return ""


def _apply_range_line_edit(lines: List[str], edit: Dict[str, Any], mode: str) -> None:
    start_raw = edit.get("start_line", edit.get("line", edit.get("line_number")))
    if start_raw is None:
        raise ValueError("line/line_number/start_line is required")
    end_raw = edit.get("end_line", start_raw)
    start = _line_number(start_raw, "start_line", len(lines))
    end = _line_number(end_raw, "end_line", len(lines))
    if end < start:
        raise ValueError("end_line must be >= start_line")
    replacement = [] if mode == "delete_line" else _edit_text(edit).splitlines()
    lines[start - 1:end] = replacement


def _apply_insert_line_edit(lines: List[str], edit: Dict[str, Any], mode: str) -> None:
    raw = edit.get("line", edit.get("line_number", edit.get("start_line")))
    if raw is None:
        raise ValueError("line/line_number is required")
    number = _line_number(raw, "line", len(lines))
    index = number - 1 if mode == "insert_before" else number
    lines[index:index] = _edit_text(edit).splitlines()


def _apply_one_skill_line_edit(lines: List[str], edit: Dict[str, Any]) -> None:
    mode = str(edit.get("mode") or "").strip().lower()
    if not mode and any(key in edit for key in ("line", "line_number", "start_line")):
        mode = "replace_line"
    if mode == "replace_all":
        lines[:] = _edit_text(edit).splitlines()
        return
    if mode in {"append", "prepend"}:
        new_lines = _edit_text(edit).splitlines()
        if mode == "append":
            lines.extend(new_lines)
        else:
            lines[:0] = new_lines
        return
    if not lines:
        raise ValueError(f"{mode or 'line edit'} requires non-empty content")

    if mode in {"replace_line", "delete_line"}:
        _apply_range_line_edit(lines, edit, mode)
        return
    if mode in {"insert_before", "insert_after"}:
        _apply_insert_line_edit(lines, edit, mode)
        return
    raise ValueError(
        "unsupported edit mode; use replace_line, insert_before, insert_after, "
        "delete_line, append, prepend, or replace_all"
    )


def _apply_skill_line_edits(content: str, arguments: Dict[str, Any]) -> tuple[str, int]:
    raw_edits = arguments.get("edits")
    if isinstance(raw_edits, list) and raw_edits:
        if not all(isinstance(item, dict) for item in raw_edits):
            raise ValueError("edits must be an array of objects")
        edits = list(raw_edits)
    else:
        edits = [arguments]
    lines = str(content or "").splitlines()
    had_trailing_newline = str(content or "").endswith("\n")
    for edit in edits:
        _apply_one_skill_line_edit(lines, edit)
    updated = "\n".join(lines)
    if had_trailing_newline and updated:
        updated += "\n"
    return updated, len(edits)


def _has_skill_line_edits(arguments: Dict[str, Any]) -> bool:
    raw_edits = arguments.get("edits")
    if isinstance(raw_edits, list) and raw_edits:
        return True
    if str(arguments.get("mode") or "").strip():
        return True
    return any(arguments.get(key) is not None for key in ("line", "line_number", "start_line"))


def _topic_entry(user_id: int, memory_id: str) -> Dict[str, Any]:
    target = str(memory_id or "").strip()
    for entry in _load_user_knowledge_entries(int(user_id)):
        if str(entry.get("memory_id") or "") == target and not target.startswith("skill:"):
            return entry
    raise ValueError("memory not found")


def read_topic_entry(*, user_id: int, memory_id: str) -> Dict[str, Any]:
    entry = _topic_entry(int(user_id), memory_id)
    detail = _entry_dict_from_file_entry(entry, with_body=True, user_id=int(user_id))
    body = str(detail.get("body") or "")
    detail.update({
        "id": memory_id,
        "slug": memory_id,
        "skill_card": body,
        "metadata": {"source": "topic", "version": None},
        "path": str(entry.get("file_path") or ""),
        "present": True,
        "lines": [
            {"line": index, "text": text}
            for index, text in enumerate(body.splitlines(), start=1)
        ],
        "line_count": len(body.splitlines()),
        "content_sha256": _text_sha256(body),
    })
    return detail


def edit_topic_entry(*, user_id: int, memory_id: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    if not _has_skill_line_edits(arguments):
        raise ValueError("ordinary topic editing requires line edits")
    detail = read_topic_entry(user_id=int(user_id), memory_id=memory_id)
    old_content = str(detail.get("skill_card") or "")
    old_sha256 = _text_sha256(old_content)
    expected = str(arguments.get("expected_sha256") or "").strip().lower()
    if expected and expected != old_sha256:
        raise ValueError("knowledge content changed after it was read; read it again before editing")
    new_content, edit_count = _apply_skill_line_edits(old_content, arguments)
    update_topic_content(user_id=int(user_id), memory_id=memory_id, content=new_content)
    return {
        "updated": True,
        "id": memory_id,
        "edit_count": edit_count,
        "endpoint_kind": "any",
        "old_sha256": old_sha256,
        "content_sha256": _text_sha256(new_content),
        "line_count": len(new_content.splitlines()),
    }


def delete_topic_entry(*, user_id: int, memory_id: str) -> Dict[str, Any]:
    delete_topic(user_id=int(user_id), memory_id=memory_id)
    return {"deleted": True, "id": memory_id}


def _managed_thought_updates(thought_id: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    updates: Dict[str, Any] = {"slug": thought_id}
    endpoint_raw = arguments.get("endpoint_kind")
    if endpoint_raw is not None and str(endpoint_raw).strip():
        updates["endpoint_kind"] = _normalize_endpoint(endpoint_raw)
    if "name" in arguments:
        display_name = str(arguments.get("name") or "").strip()
        if not display_name:
            raise ValueError("name must not be empty")
        updates["displayName"] = display_name
    if "summary" in arguments:
        updates["summary"] = str(arguments.get("summary") or "").strip()
    if "triggers" in arguments:
        updates["triggers"] = _normalize_triggers(arguments.get("triggers"))
    return updates


def edit_managed_thought(
    *,
    user_id: int,
    thought_id: str,
    arguments: Dict[str, Any],
    found: tuple[str, str, Dict[str, Any], str],
) -> Dict[str, Any]:
    """Apply content and metadata edits to a managed skill snapshot."""
    has_line_edits = _has_skill_line_edits(arguments)
    has_endpoint = bool(str(arguments.get("endpoint_kind") or "").strip())
    has_metadata = has_endpoint or any(key in arguments for key in ("name", "summary", "triggers"))
    if not has_line_edits and not has_metadata:
        raise ValueError("nothing to edit: provide line edits, metadata, and/or endpoint_kind")

    old_content = str(found[3] or "")
    old_sha256 = _text_sha256(old_content)
    new_content, edit_count = old_content, 0
    if has_line_edits:
        expected = str(arguments.get("expected_sha256") or "").strip().lower()
        if expected and expected != old_sha256:
            raise ValueError("SKILL.md changed after it was read; read it again before editing")
        new_content, edit_count = _apply_skill_line_edits(old_content, arguments)

    updates = _managed_thought_updates(thought_id, arguments)
    merged = _upsert_thought(int(user_id), updates, body=new_content if has_line_edits else None)
    return {
        "updated": True,
        "id": thought_id,
        "edit_count": edit_count,
        "name": str(merged.get("displayName") or ""),
        "summary": str(merged.get("summary") or ""),
        "triggers": merged.get("triggers") or [],
        "endpoint_kind": _normalize_endpoint(merged.get("endpoint_kind")),
        "old_sha256": old_sha256,
        "content_sha256": _text_sha256(new_content),
        "line_count": len(new_content.splitlines()),
    }
