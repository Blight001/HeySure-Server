"""librarian_thoughts — 传承思想/技能 CRUD + NPX/全局技能安装。"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

import yaml

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
from .librarian_entry_crud import (
    _text_sha256,
    delete_topic_entry,
    edit_managed_thought,
    edit_topic_entry,
    read_topic_entry,
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
        if str(entry.get("kind") or "") == "workspace":
            installed.append({
                "slug": memory_id,
                "memory_id": memory_id,
                "displayName": str(entry.get("title") or memory_id),
                "summary": str(entry.get("summary") or ""),
                "triggers": entry.get("triggers") or [],
                "version": None,
                "ownerHandle": str(entry.get("owner_handle") or ""),
                "source": "workspace",
                "path": str(entry.get("file_path") or ""),
                "installed_at": float(entry.get("created_at") or entry.get("updated_at") or 0),
                "auto_enabled": False,
                "endpoint_kind": str(entry.get("endpoint_kind") or "any"),
                "scope": str(entry.get("scope") or "ai"),
                "scope_target": entry.get("scope_target"),
                "owner_ai_config_id": entry.get("source_ai_config_id"),
                "present": True,
                "kind": "workspace",
            })
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
        "description": "传承知识与 Skill 以本地文件为真相源：KnowledgeBase/topics/ 下的共享条目，以及每个 AI 工作区 skills/*/SKILL.md；可由 AI 主动创建或从 ClawHub / npx skills 安装。",
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


def read_inheritance_thought(
    *, user_id: int, thought_id: str, ai_config_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Return one installed inheritance thought by the ID emitted by the list."""
    if _find_thought(int(user_id), thought_id) is None:
        from .workspace_skills import read_workspace_skill

        try:
            return read_workspace_skill(
                int(user_id), thought_id, ai_config_id=ai_config_id,
            )
        except PermissionError:
            raise
        except ValueError:
            pass
        return read_topic_entry(user_id=int(user_id), memory_id=thought_id)
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


def edit_inheritance_thought(
    *,
    user_id: int,
    thought_id: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Edit either a managed skill snapshot or an ordinary procedural topic."""
    raw_id = str(thought_id or "").strip()
    found = _find_thought(int(user_id), raw_id)
    if found is None:
        return edit_topic_entry(user_id=int(user_id), memory_id=raw_id, arguments=arguments)
    return edit_managed_thought(
        user_id=int(user_id), thought_id=raw_id, arguments=arguments, found=found,
    )


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
    triggers: Optional[List[str]] = None,
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
    trigger_source: Any = triggers
    if triggers is None:
        trigger_source = _extract_skill_triggers(body, name)
    triggers_norm = _normalize_triggers(trigger_source)

    resolved_endpoint = _resolve_endpoint_kind(int(user_id), ai_config_id, endpoint_kind)
    row = {
        "slug": thought_id,
        "displayName": name,
        "summary": summary_text,
        "triggers": triggers_norm,
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
    if _find_thought(int(user_id), thought_id) is None:
        return delete_topic_entry(user_id=int(user_id), memory_id=thought_id)
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
