"""File-backed knowledge entry adapters used by the librarian core."""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from . import librarian_core as _core
from .librarian_core import (
    _THOUGHT_MARKER_KEY,
    _TOPICS_DIR,
    _clawhub_installed_items,
    _normalize_endpoint,
    _parse_triggers_field,
    _read_text,
    _split_frontmatter,
    _topic_path,
    _unquote_scalar,
    _yaml_frontmatter,
)
from .workspace_skills import list_workspace_skills


def _first_value(mapping: Dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


def _optional_int(value: Any) -> Optional[int]:
    if value in (None, "", "null"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in ("true", "1", "yes")


def _thought_meta_to_row(meta: Dict[str, Any], rel_path: str) -> Dict[str, Any]:
    """Convert a thought frontmatter mapping into the installed-row shape."""
    row: Dict[str, Any] = {
        "slug": str(_first_value(meta, "slug")).strip(),
        "displayName": str(_first_value(meta, "name", "title", "slug")).strip(),
        "summary": str(_first_value(meta, "description", "summary")).strip(),
        "triggers": _parse_triggers_field(_first_value(meta, "triggers", "keywords", "tags")),
        "version": _normalize_version(meta.get("version")),
        "ownerHandle": str(_first_value(meta, "owner", "ownerHandle")).strip(),
        "source": str(_first_value(meta, "source", default="manual")),
        "path": rel_path,
        "installed_at": _safe_float(meta.get("installed_at")),
        "auto_enabled": _is_truthy(meta.get("auto_enabled")),
        "endpoint_kind": _normalize_endpoint(meta.get("endpoint_kind")),
        "scope": str(_first_value(meta, "scope", default="global")),
        "scope_target": str(_first_value(meta, "scope_target")).strip() or None,
        "owner_ai_config_id": _optional_int(meta.get("owner_ai_config_id")),
        "source_plan_id": str(_first_value(meta, "source_plan_id")).strip() or None,
        "trust": {"verdict": str(_first_value(meta, "trust_verdict"))},
    }
    registry_url = str(_first_value(meta, "registry_url")).strip()
    if registry_url:
        row["registry_url"] = registry_url
    return row


def _normalize_version(value: Any) -> Any:
    if isinstance(value, str) and value.strip().lower() in ("", "null", "none"):
        return None
    return value


def _thought_frontmatter(row: Dict[str, Any]) -> Dict[str, Any]:
    trust = row.get("trust") if isinstance(row.get("trust"), dict) else {}
    triggers = row.get("triggers")
    if not isinstance(triggers, list):
        triggers = _parse_triggers_field(triggers)
    return {
        "name": str(_first_value(row, "displayName", "slug")),
        "description": str(_first_value(row, "summary")),
        "triggers": triggers,
        _THOUGHT_MARKER_KEY: True,
        "slug": str(_first_value(row, "slug")),
        "source": str(_first_value(row, "source", default="manual")),
        "endpoint_kind": _normalize_endpoint(row.get("endpoint_kind")),
        "scope": str(_first_value(row, "scope", default="global")),
        "scope_target": row.get("scope_target"),
        "owner_ai_config_id": row.get("owner_ai_config_id"),
        "source_plan_id": row.get("source_plan_id"),
        "evolved_at": row.get("evolved_at"),
        "version": row.get("version"),
        "owner": str(_first_value(row, "ownerHandle")),
        "installed_at": _safe_float(row.get("installed_at"), time.time()),
        "auto_enabled": bool(row.get("auto_enabled")),
        "trust_verdict": str(_first_value(trust, "verdict") or _first_value(row, "trust_verdict")),
    }


def _render_thought_md(row: Dict[str, Any], body: str) -> str:
    """Render installed-row metadata and body as one thought Markdown file."""
    frontmatter = _thought_frontmatter(row)
    registry_url = str(_first_value(row, "registry_url")).strip()
    if registry_url:
        frontmatter["registry_url"] = registry_url
    return _yaml_frontmatter(frontmatter) + "\n\n" + str(body or "").strip() + "\n"


def _topic_entry(user_id: int, fname: str) -> Optional[Dict[str, Any]]:
    rel = f"{_TOPICS_DIR}/{fname}".replace("\\", "/")
    path = _topic_path(user_id, rel)
    raw = _read_text(path)
    if not raw:
        return None
    raw_meta, body = _split_frontmatter(raw)
    meta = {key: _unquote_scalar(value) for key, value in raw_meta.items()}
    if _is_truthy(meta.get(_THOUGHT_MARKER_KEY)):
        return None
    memory_id = str(_first_value(meta, "memory_id", default=f"topic:{os.path.splitext(fname)[0]}")).strip()
    title = str(_first_value(meta, "title", default=os.path.splitext(fname)[0]))
    mtime = _safe_mtime(path)
    return {
        "memory_id": memory_id,
        "title": title,
        "triggers": _parse_triggers_field(_first_value(meta, "triggers", "keywords")),
        "scope": str(_first_value(meta, "scope", default="global")),
        "scope_target": meta.get("scope_target"),
        "status": str(_first_value(meta, "status", default="active")),
        "confidence": _safe_float(meta.get("confidence"), 1.0),
        "use_count": 0,
        "last_used_at": None,
        "file_path": rel,
        "summary": str(_first_value(meta, "summary", default=(body or "")[:200])).strip(),
        "source_job_id": meta.get("source_job_id"),
        "source_generation": meta.get("source_generation"),
        "source_ai_config_id": meta.get("source_ai_config_id"),
        "source_message_id": meta.get("source_message_id"),
        "created_at": _safe_float(meta.get("created_at"), mtime),
        "updated_at": _safe_float(meta.get("updated_at"), mtime),
    }


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return time.time()


def _load_topic_entries(user_id: int) -> List[Dict[str, Any]]:
    directory = os.path.join(_core._kb_root(user_id), _TOPICS_DIR)
    if not os.path.isdir(directory):
        return []
    try:
        names = sorted(os.listdir(directory))
        return [entry for name in names if name.lower().endswith(".md")
                if (entry := _topic_entry(user_id, name)) is not None]
    except Exception as exc:
        import logging
        logging.getLogger(__name__).info("topic scan failed user=%s: %s", user_id, exc)
        return []


def _installed_entry(user_id: int, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    slug = str(_first_value(item, "slug")).strip()
    skill_rel = str(_first_value(item, "path")).strip().replace("\\", "/")
    if not slug or not skill_rel or not os.path.exists(_topic_path(user_id, skill_rel)):
        return None
    installed_at = _safe_float(item.get("installed_at"))
    return {
        "memory_id": f"skill:{slug}",
        "title": str(_first_value(item, "displayName", default=slug)),
        "triggers": item.get("triggers") or [],
        "scope": str(_first_value(item, "scope", default="global")),
        "scope_target": item.get("scope_target") or item.get("owner_ai_config_id"),
        "status": "active",
        "confidence": 1.0,
        "use_count": 0,
        "last_used_at": None,
        "file_path": skill_rel,
        "summary": str(_first_value(item, "summary", "description")),
        "source_job_id": item.get("source_plan_id"),
        "source_generation": None,
        "source_ai_config_id": None,
        "source_message_id": None,
        "created_at": installed_at or time.time(),
        "updated_at": installed_at or time.time(),
    }


def _load_installed_entries(user_id: int) -> List[Dict[str, Any]]:
    try:
        return [entry for item in _clawhub_installed_items(user_id)
                if (entry := _installed_entry(user_id, item)) is not None]
    except Exception as exc:
        import logging
        logging.getLogger(__name__).info("installed skill scan failed user=%s: %s", user_id, exc)
        return []


def _workspace_entry(item: Dict[str, Any]) -> Dict[str, Any]:
    installed_at = _safe_float(item.get("installed_at"), time.time())
    slug = item["slug"]
    return {
        "memory_id": slug,
        "title": item.get("displayName") or slug,
        "triggers": item.get("triggers") or [],
        "scope": item.get("scope") or "ai",
        "scope_target": item.get("scope_target"),
        "status": "active",
        "confidence": 1.0,
        "use_count": 0,
        "last_used_at": None,
        "file_path": item.get("file_path") or "",
        "summary": item.get("summary") or "",
        "source_job_id": None,
        "source_generation": None,
        "source_ai_config_id": item.get("owner_ai_config_id"),
        "source_message_id": None,
        "created_at": installed_at,
        "updated_at": installed_at,
        "source": "workspace",
        "kind": "workspace",
        "endpoint_kind": item.get("endpoint_kind") or "any",
        "owner_handle": item.get("ownerHandle") or "",
        "_absolute_path": item.get("_absolute_path"),
    }


def _load_workspace_entries(user_id: int) -> List[Dict[str, Any]]:
    try:
        return [_workspace_entry(item) for item in list_workspace_skills(int(user_id))]
    except Exception as exc:
        import logging
        logging.getLogger(__name__).info("workspace skill scan failed user=%s: %s", user_id, exc)
        return []


def _load_user_knowledge_entries(user_id: int) -> List[Dict[str, Any]]:
    """Scan topic, installed Skill, and AI workspace files into API entries."""
    entries: List[Dict[str, Any]] = []
    entries.extend(_load_topic_entries(user_id))
    entries.extend(_load_installed_entries(user_id))
    entries.extend(_load_workspace_entries(user_id))
    return entries


def _entry_dict_from_file_entry(
    entry: Dict[str, Any], *, with_body: bool = False, user_id: Optional[int] = None
) -> Dict[str, Any]:
    fields = (
        "memory_id", "title", "triggers", "scope", "scope_target", "status",
        "confidence", "use_count", "last_used_at", "file_path", "summary",
        "source_job_id", "source_generation", "source_ai_config_id", "source_message_id",
        "created_at", "updated_at",
    )
    output = {key: entry.get(key) for key in fields}
    if with_body and user_id is not None:
        output["body"] = _entry_body(entry, user_id)
    return output


def _entry_body(entry: Dict[str, Any], user_id: int) -> str:
    try:
        absolute = entry.get("_absolute_path")
        raw = _read_text(str(absolute)) if absolute else None
        if raw is None and entry.get("file_path"):
            raw = _read_text(_topic_path(user_id, entry["file_path"]))
        if raw is not None:
            _meta, body = _split_frontmatter(raw)
            return body
    except Exception:
        pass
    return ""
