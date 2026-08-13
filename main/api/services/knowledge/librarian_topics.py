"""普通传承知识（KnowledgeBase/topics/*.md）的控制台 CRUD。"""

from __future__ import annotations

import os
from typing import Any, Dict

from .librarian_core import (
    _TOPICS_DIR,
    _entry_dict_from_file_entry,
    _load_user_knowledge_entries,
    _read_text,
    _rebuild_index,
    _safe_write,
    _topic_path,
)


def _editable_topic(*, user_id: int, memory_id: str) -> tuple[Dict[str, Any], str, str]:
    """定位可由控制台编辑的普通 topics/*.md 条目。"""
    target_id = str(memory_id or "").strip()
    if not target_id or target_id.startswith("builtin.") or target_id.startswith("skill:"):
        raise ValueError("knowledge entry is not editable")
    for entry in _load_user_knowledge_entries(user_id):
        if str(entry.get("memory_id") or "") != target_id:
            continue
        file_rel = str(entry.get("file_path") or "").replace("\\", "/")
        if not file_rel.startswith(f"{_TOPICS_DIR}/") or not file_rel.lower().endswith(".md"):
            raise ValueError("knowledge entry is not editable")
        path = _topic_path(user_id, file_rel)
        raw = _read_text(path)
        if raw is None:
            raise ValueError("memory not found")
        return entry, path, raw
    raise ValueError("memory not found")


def update_topic_content(*, user_id: int, memory_id: str, content: str) -> Dict[str, Any]:
    """只替换普通传承知识正文，保留其 frontmatter 元数据。"""
    _entry, path, raw = _editable_topic(user_id=user_id, memory_id=memory_id)
    body = str(content or "")
    marker = raw.find("\n---\n", 4) if raw.startswith("---\n") else -1
    updated = raw[:marker + 5] + body if marker >= 0 else body
    _safe_write(path, updated)
    _rebuild_index(user_id)
    for entry in _load_user_knowledge_entries(user_id):
        if str(entry.get("memory_id") or "") == memory_id:
            return _entry_dict_from_file_entry(entry, with_body=True, user_id=user_id)
    raise ValueError("memory not found")


def delete_topic(*, user_id: int, memory_id: str) -> Dict[str, Any]:
    """永久删除一条普通传承知识文件。"""
    _entry, path, _raw = _editable_topic(user_id=user_id, memory_id=memory_id)
    try:
        os.remove(path)
    except FileNotFoundError as exc:
        raise ValueError("memory not found") from exc
    _rebuild_index(user_id)
    return {"deleted": True, "memory_id": memory_id}
