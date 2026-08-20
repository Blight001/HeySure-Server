"""librarian_thoughts — 传承思想/技能 CRUD + NPX/全局技能安装。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from mcp_runtime.mcp.core import safe_join
import logging

from .librarian_core import (
    _kb_root,
    _slugify,
    _normalize_endpoint,
    _resolve_endpoint_kind,
    _TOPICS_DIR,
    _normalize_triggers,
    _parse_triggers_field,
    _safe_write,
    _topic_path,
    _read_text,
    _split_frontmatter,
    _clawhub_installed_items,
    _load_user_knowledge_entries,
    _upsert_thought,
    _delete_thought_file,
    _find_thought,
    _entry_dict_from_file_entry,
)
from ...integrations import clawhub

logger = logging.getLogger(__name__)


def _inheritance_thoughts_payload(user_id: int) -> Dict[str, Any]:
    installed = _clawhub_installed_items(user_id)
    thought_paths = {
        str(item.get("path") or "").strip().replace("\\", "/")
        for item in installed
        if str(item.get("path") or "").strip()
    }
    for entry in _load_user_knowledge_entries(user_id):
        path = str(entry.get("file_path") or "").strip().replace("\\", "/")
        if not path or path in thought_paths:
            continue
        if str(entry.get("status") or "active") != "active":
            continue
        memory_id = str(entry.get("memory_id") or "").strip()
        if not memory_id:
            continue
        installed.append({
            "slug": memory_id,
            "memory_id": memory_id,
            "displayName": str(entry.get("title") or memory_id),
            "summary": str(entry.get("summary") or ""),
            "triggers": entry.get("triggers") or [],
            "version": None,
            "ownerHandle": "",
            "source": "topic",
            "path": path,
            "installed_at": float(entry.get("created_at") or entry.get("updated_at") or 0),
            "auto_enabled": False,
            "endpoint_kind": "any",
            "present": True,
            "kind": "knowledge",
        })
    installed.sort(key=lambda item: float(item.get("installed_at") or 0), reverse=True)
    return {
        "description": "传承知识以单文件 .md 落盘在 KnowledgeBase/topics/ 下（frontmatter 即元数据），可由 AI 主动创建或从 ClawHub / npx skills 安装；运行时只使用本地文件。",
        "registry_url": clawhub.registry_base_url(),
        "storage_root": _TOPICS_DIR,
        "installed_total": len(installed),
        "installed": installed,
    }


def list_inheritance_thoughts(
    *,
    user_id: int,
    query: str = "",
    limit: int = 20,
    offset: int = 0,
    compact: bool = True,
) -> Dict[str, Any]:
    """Return a filtered, paginated list of inheritance thoughts.

    Compact output is the MCP-friendly default: it keeps only fields needed to
    choose an item for ``get_thought``.  Callers that need registry metadata can
    explicitly request ``compact=False``.
    """
    payload = _inheritance_thoughts_payload(int(user_id))
    normalized_query = str(query or "").strip().casefold()
    matched = [
        item
        for installed in payload.get("installed") or []
        if (item := _listed_thought(installed, normalized_query, compact)) is not None
    ]

    total = len(matched)
    page = matched[offset:offset + limit]
    returned = len(page)
    return {
        "items": page,
        "total": total,
        "returned": returned,
        "offset": offset,
        "limit": limit,
        "has_more": offset + returned < total,
        "next_offset": offset + returned if offset + returned < total else None,
        "compact": compact,
        "query": query or "",
        "hint": "Use get_thought with an item id to read full content; increase offset when has_more=true.",
        **({
            "description": payload.get("description"),
            "storage_root": payload.get("storage_root"),
        } if not compact else {}),
    }


def _listed_thought(installed: Dict[str, Any], query: str, compact: bool) -> Optional[Dict[str, Any]]:
    item = dict(installed)
    item["id"] = str(item.get("slug") or "")
    searchable = " ".join((
        item["id"], str(item.get("displayName") or ""), str(item.get("summary") or ""),
        " ".join(str(value) for value in (item.get("triggers") or [])),
    )).casefold()
    if query and query not in searchable:
        return None
    if not compact:
        return item
    return {
        "id": item["id"], "name": str(item.get("displayName") or item["id"]),
        "summary": str(item.get("summary") or "")[:240],
        "source": str(item.get("source") or ""),
        "endpoint_kind": str(item.get("endpoint_kind") or "any"),
    }


def read_inheritance_thought(*, user_id: int, thought_id: str) -> Dict[str, Any]:
    """Return one installed inheritance thought by the ID emitted by the list."""
    from .librarian_clawhub import clawhub_installed_skill_detail
    detail = clawhub_installed_skill_detail(
        user_id=int(user_id),
        slug=str(thought_id or "").strip(),
    )
    detail["id"] = str(detail.get("slug") or thought_id)
    content = str(detail.get("skill_card") or "")
    detail["lines"] = [
        {"line": index, "text": text}
        for index, text in enumerate(content.splitlines(), start=1)
    ]
    detail["line_count"] = len(detail["lines"])
    detail["content_sha256"] = _text_sha256(content)
    return detail


def _text_sha256(content: str) -> str:
    import hashlib

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
        return
    if mode in {"insert_before", "insert_after"}:
        raw = edit.get("line", edit.get("line_number", edit.get("start_line")))
        if raw is None:
            raise ValueError("line/line_number is required")
        number = _line_number(raw, "line", len(lines))
        index = number - 1 if mode == "insert_before" else number
        lines[index:index] = _edit_text(edit).splitlines()
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
    """是否带有 SKILL.md 行编辑指令（区别于纯改端）。"""
    raw_edits = arguments.get("edits")
    if isinstance(raw_edits, list) and raw_edits:
        return True
    if str(arguments.get("mode") or "").strip():
        return True
    return any(arguments.get(key) is not None for key in ("line", "line_number", "start_line"))


def edit_inheritance_thought(
    *,
    user_id: int,
    thought_id: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """按行编辑正文与/或改端（endpoint_kind）。两者均可单独使用。"""
    from .librarian_clawhub import _normalize_clawhub_slug
    thought_id = _normalize_clawhub_slug(thought_id)
    endpoint_raw = arguments.get("endpoint_kind")
    has_endpoint = endpoint_raw is not None and str(endpoint_raw).strip() != ""
    has_line_edits = _has_skill_line_edits(arguments)
    if not has_line_edits and not has_endpoint:
        raise ValueError("nothing to edit: provide line edits and/or endpoint_kind")

    found = _find_thought(int(user_id), thought_id)
    if found is None:
        raise ValueError("installed skill not found")
    _rel, _abs, _meta, old_body = found
    old_content = str(old_body or "")
    old_sha256 = _text_sha256(old_content)
    new_content = old_content
    edit_count = 0

    update_row: Dict[str, Any] = {"slug": thought_id}
    body_arg = None
    if has_line_edits:
        expected = str(arguments.get("expected_sha256") or "").strip().lower()
        if expected and expected != old_sha256:
            raise ValueError("SKILL.md changed after it was read; read it again before editing")
        new_content, edit_count = _apply_skill_line_edits(old_content, arguments)
        body_arg = new_content
    if has_endpoint:
        update_row["endpoint_kind"] = _normalize_endpoint(endpoint_raw)

    merged = _upsert_thought(int(user_id), update_row, body=body_arg)
    return {
        "updated": True,
        "id": thought_id,
        "edit_count": edit_count,
        "endpoint_kind": _normalize_endpoint(merged.get("endpoint_kind")),
        "old_sha256": old_sha256,
        "content_sha256": _text_sha256(new_content),
        "line_count": len(new_content.splitlines()),
    }


def _ensure_skill_frontmatter(body: str, *, name: str, description: str) -> str:
    """确保 SKILL.md 带 name/description frontmatter，便于元数据解析与运行时使用。"""
    text = str(body or "")
    if text.lstrip().startswith("---"):
        return text if text.endswith("\n") else text + "\n"
    lines = ["---", f"name: {json.dumps(name, ensure_ascii=False)}"]
    if description:
        lines.append(f"description: {json.dumps(description, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + text.strip() + "\n"


def _extract_skill_triggers(skill_md_text: str, name: str) -> str:
    """从 SKILL.md 提取触发词。
    优先读 frontmatter keywords/tags 字段；
    若没有，以 name 的词作为触发词兜底。
    """
    text = str(skill_md_text or "")
    candidates: List[str] = []
    if text.lstrip().startswith("---"):
        try:
            end = text.find("\n---", 3)
            if end >= 0:
                head = text[3:end]
                try:
                    meta = yaml.safe_load(head)
                    if isinstance(meta, dict):
                        for key in ("keywords", "tags", "triggers", "trigger_words"):
                            val = meta.get(key)
                            if val is not None:
                                candidates.extend(_normalize_triggers(val))
                                break
                except Exception:
                    pass
        except Exception:
            pass
    if not candidates:
        # fallback to name words (split on spaces/punct as word triggers)
        norm_name = re.sub(r'[\s,，;；/\\\-_.]+', ',', str(name or ""))
        candidates = _normalize_triggers(norm_name)
    return ",".join(candidates)


def _sync_skill_to_knowledge_entry(
    user_id: int,
    slug: str,
    name: str,
    summary: str,
    skill_md_path: str,     # 相对 KnowledgeBase/ 的路径
    installed_at: float,
    *,
    ai_config_id: Optional[int] = None,
    status: str = "active",
) -> Dict[str, Any]:
    """将一个 skill 登记（文件已存在时）。纯文件驱动，不再写 KnowledgeEntry 表。
    返回 entry dict 用于调用方。
    """
    memory_id = f"skill:{slug}"

    # Sanitize file_path
    safe_path = str(skill_md_path or "").strip().lstrip("/\\")
    if ".." in safe_path.replace("\\", "/").split("/"):
        safe_path = ""
    if safe_path and not safe_path.lower().endswith((".md", "skill.md")):
        safe_path = safe_path.rstrip("/\\") + "/SKILL.md"
    skill_md_path = safe_path

    raw = _read_text(_topic_path(user_id, skill_md_path)) if skill_md_path else None
    triggers = _extract_skill_triggers(raw or "", name)
    now = time.time()

    # Build a dict (file-backed "entry")
    entry_dict = {
        "memory_id": memory_id,
        "title": name,
        "triggers": triggers if isinstance(triggers, list) else _parse_triggers_field(triggers),
        "scope": "global",
        "scope_target": None,
        "status": status,
        "confidence": 1.0,
        "use_count": 0,
        "last_used_at": None,
        "file_path": skill_md_path,
        "summary": summary,
        "source_job_id": None,
        "source_generation": None,
        "source_ai_config_id": None,
        "source_message_id": None,
        "created_at": installed_at,
        "updated_at": now,
    }

    return {
        "installed": True,
        "slug": slug,
        "entry": _entry_dict_from_file_entry(entry_dict, with_body=False, user_id=user_id),
    }


def create_inheritance_thought(
    *,
    user_id: int,
    name: str,
    content: str,
    summary: Optional[str] = None,
    endpoint_kind: Optional[str] = None,
    ai_config_id: Optional[int] = None,
) -> Dict[str, Any]:
    """主动创建一条传承思想：AI 直接写 SKILL.md，落本地快照并登记到传承思想库。

    与安装路径（ClawHub/npx）并列，``source="manual"``。``endpoint_kind`` 端归类
    显式优先，未传按安装成员当前绑定的端侧 agent 自动推断。
    """
    import hashlib

    name = str(name or "").strip()
    if not name:
        raise ValueError("name is required")
    body = str(content or "").strip()
    if not body:
        raise ValueError("content is required")

    slug_base = _slugify(name) or "skill"
    suffix = hashlib.sha1(f"{name}-{time.time()}".encode("utf-8")).hexdigest()[:8]
    thought_id = f"manual/{slug_base}-{suffix}"

    # 正文若自带 frontmatter（旧 SKILL.md 形态），剥离后只留正文，统一并入单块 frontmatter。
    fm, stripped = _split_frontmatter(body)
    body_text = stripped if fm else body
    summary_text = str(summary or "").strip()
    if not summary_text and isinstance(fm, dict):
        summary_text = str(fm.get("description") or "").strip()

    resolved_endpoint = _resolve_endpoint_kind(int(user_id), ai_config_id, endpoint_kind)
    row = {
        "slug": thought_id,
        "displayName": name,
        "summary": summary_text,
        "version": None,
        "ownerHandle": "",
        "source": "manual",
        "installed_at": time.time(),
        "auto_enabled": False,
        "endpoint_kind": resolved_endpoint,
        "trust": {"verdict": "self-authored"},
    }
    merged = _upsert_thought(int(user_id), row, body=body_text)
    return dict(merged, id=thought_id)


def delete_inheritance_thought(*, user_id: int, thought_id: str) -> Dict[str, Any]:
    from .librarian_clawhub import delete_clawhub_installed_skill
    result = delete_clawhub_installed_skill(
        user_id=int(user_id),
        slug=str(thought_id or "").strip(),
    )
    return {"deleted": True, "id": str(result.get("slug") or thought_id)}


def _render_inheritance_thoughts_body(payload: Dict[str, Any]) -> str:
    lines = [
        "# 传承知识",
        "",
        str(payload.get("description") or ""),
        "",
        f"ClawHub：{payload.get('registry_url') or ''}",
        f"本地目录：KnowledgeBase/{payload.get('storage_root') or ''}",
        f"已安装：{int(payload.get('installed_total') or 0)}",
        "",
    ]
    installed = payload.get("installed") if isinstance(payload.get("installed"), list) else []
    if installed:
        lines.append("## 已安装 / 已沉淀条目")
        lines.append("")
        for item in installed:
            slug = str(item.get("slug") or "")
            name = str(item.get("displayName") or slug)
            version = str(item.get("version") or ("knowledge" if item.get("kind") == "knowledge" else "latest"))
            owner = str(item.get("ownerHandle") or "")
            present = "可用" if item.get("present") else "文件缺失"
            lines.append(f"- `{slug}` {name} · {version} · {owner} · {present}")
            summary = str(item.get("summary") or "").strip()
            if summary:
                lines.append(f"  - {summary}")
        lines.append("")
    else:
        lines.append("暂无已安装 ClawHub 技能。")
    return "\n".join(lines).strip()
