"""Discovery and safe loading of AI-member workspace Skills.

Workspace Skills are intentionally file-first: an AI can create
``skills/<slug>/SKILL.md`` without a second registration step.  This module
only scans the skills directory of configured AI members, so arbitrary files
elsewhere in a workspace never become executable or injectable Skill content.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlmodel import Session, select

from ...database import engine
from ...models import AssistantAIConfig
from ...services.storage.workspace_scope import member_workspace_dir
WORKSPACE_SKILLS_DIR = "skills"
WORKSPACE_SKILL_PREFIX = "workspace:"
MAX_WORKSPACE_SKILL_CARD_BYTES = 512 * 1024


def _slugify(title: str) -> str:
    cleaned = re.sub(r"[^0-9a-z一-鿿]+", "-", str(title or "").strip().lower()).strip("-")
    return (cleaned or "untitled")[:80]


def _normalize_triggers(value: Any) -> List[str]:
    if isinstance(value, list):
        items = [str(x).strip() for x in value if str(x).strip()]
    elif isinstance(value, str):
        items = [piece.strip() for piece in re.split(r"[,，;；\n]+", value) if piece.strip()]
    else:
        items = []
    seen = set()
    result: List[str] = []
    for item in items:
        if item.casefold() in seen:
            continue
        seen.add(item.casefold())
        result.append(item)
    return result[:20]


def _parse_triggers_field(raw: Any) -> List[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    value = str(raw or "").strip()
    if value.startswith("[") and value.endswith("]"):
        return [part.strip().strip("\"'") for part in value[1:-1].split(",") if part.strip()]
    return [part.strip() for part in re.split(r"[,，;；\n]+", value) if part.strip()]


def _normalize_endpoint(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"desktop", "windows", "linux", "desktop_windows", "desktop_linux"}:
        return "desktop"
    if raw in {"browser", "extension", "browser_extension", "browser-extension"}:
        return "browser"
    return "any"


def _split_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    src = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not src.startswith("---\n"):
        return {}, src
    end = src.find("\n---\n", 4)
    if end < 0:
        return {}, src
    meta: Dict[str, Any] = {}
    for line in src[4:end].split("\n"):
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta, src[end + 5:].lstrip("\n")


def _unquote(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _safe_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _first_heading_or_text(body: str, fallback: str) -> str:
    for line in str(body or "").splitlines():
        text = line.strip().lstrip("#").strip()
        if text:
            return text[:120]
    return fallback


def _skill_description(meta: Dict[str, Any], body: str) -> str:
    description = str(_unquote(meta.get("description") or meta.get("summary") or "")).strip()
    if description:
        return description[:240]
    for line in str(body or "").splitlines():
        text = line.strip()
        if text and not text.startswith("#"):
            return text[:240]
    return ""


def _skill_slug(ai_config_id: int, directory_name: str) -> str:
    base = _slugify(directory_name) or "skill"
    digest = hashlib.sha1(directory_name.encode("utf-8")).hexdigest()[:8]
    return f"{WORKSPACE_SKILL_PREFIX}{int(ai_config_id)}:{base}-{digest}"


def _configured_members(user_id: int) -> List[AssistantAIConfig]:
    with Session(engine) as session:
        return list(session.exec(
            select(AssistantAIConfig).where(AssistantAIConfig.user_id == int(user_id))
        ).all())


def _candidate_cards(root: str) -> Iterable[Tuple[str, str]]:
    skills_root = os.path.join(root, WORKSPACE_SKILLS_DIR)
    if not os.path.isdir(skills_root) or os.path.islink(skills_root):
        return
    try:
        children = sorted(os.scandir(skills_root), key=lambda item: item.name.casefold())
    except OSError:
        return
    for child in children:
        if not child.is_dir(follow_symlinks=False):
            continue
        card = os.path.join(child.path, "SKILL.md")
        if os.path.islink(card) or not os.path.isfile(card):
            continue
        yield child.name, card


def _build_item(
    *,
    user_id: int,
    ai_config_id: int,
    ai_name: str,
    directory_name: str,
    card_path: str,
) -> Optional[Dict[str, Any]]:
    try:
        stat = os.stat(card_path, follow_symlinks=False)
        if stat.st_size <= 0 or stat.st_size > MAX_WORKSPACE_SKILL_CARD_BYTES:
            return None
        with open(card_path, "r", encoding="utf-8") as handle:
            raw = handle.read(MAX_WORKSPACE_SKILL_CARD_BYTES + 1)
    except (OSError, UnicodeError):
        return None
    if len(raw.encode("utf-8")) > MAX_WORKSPACE_SKILL_CARD_BYTES:
        return None
    meta_raw, body = _split_frontmatter(raw)
    meta = {str(key): _unquote(value) for key, value in meta_raw.items()}
    fallback_name = _first_heading_or_text(body, directory_name)
    name = str(meta.get("name") or meta.get("title") or fallback_name).strip()[:120]
    if not name:
        return None
    summary = _skill_description(meta, body)
    trigger_value = meta.get("triggers") or meta.get("keywords") or meta.get("tags") or name
    triggers = _normalize_triggers(_parse_triggers_field(trigger_value))
    scope = str(meta.get("scope") or "ai").strip().lower()
    if scope != "global":
        scope = "ai"
    owner_id = int(ai_config_id)
    scope_target = None if scope == "global" else str(owner_id)
    try:
        relative = os.path.relpath(card_path, member_workspace_dir(user_id, owner_id, create=False))
    except (OSError, ValueError):
        relative = f"{WORKSPACE_SKILLS_DIR}/{directory_name}/SKILL.md"
    relative = relative.replace(os.sep, "/")
    reference = _skill_slug(owner_id, directory_name)
    installed_at = _safe_float(meta.get("installed_at") or meta.get("created_at"), stat.st_mtime)
    return {
        "slug": reference,
        "memory_id": reference,
        "displayName": name,
        "summary": summary,
        "triggers": triggers,
        "version": str(meta.get("version") or "").strip() or None,
        "ownerHandle": ai_name,
        "source": "workspace",
        "path": relative,
        "file_path": relative,
        "_absolute_path": card_path,
        "installed_at": installed_at,
        "auto_enabled": False,
        "endpoint_kind": _normalize_endpoint(meta.get("endpoint_kind")),
        "scope": scope,
        "scope_target": scope_target,
        "owner_ai_config_id": owner_id,
        "trust": {"verdict": "self-authored"},
        "present": True,
        "status": "active",
        "kind": "workspace",
        "user_id": int(user_id),
    }


def list_workspace_skills(
    user_id: int,
    *,
    ai_config_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Discover current workspace Skills and apply visibility rules.

    With an AI id, that AI sees its private Skills plus explicitly global
    Skills from other members.  Without an AI id, the knowledge panel receives
    all member Skills for inspection by the account owner.
    """
    target_id = int(ai_config_id) if ai_config_id is not None else None
    result: List[Dict[str, Any]] = []
    for config in _configured_members(int(user_id)):
        owner_id = int(config.id)
        if not owner_id:
            continue
        root = member_workspace_dir(int(user_id), owner_id, create=False)
        for directory_name, card_path in _candidate_cards(root):
            item = _build_item(
                user_id=int(user_id),
                ai_config_id=owner_id,
                ai_name=str(config.name or ""),
                directory_name=directory_name,
                card_path=card_path,
            )
            if item is None:
                continue
            if target_id is not None and item["scope"] != "global" and owner_id != target_id:
                continue
            result.append(item)
    result.sort(key=lambda row: (str(row.get("displayName") or "").casefold(), str(row["slug"])))
    return result


def find_workspace_skill(user_id: int, reference: str) -> Optional[Dict[str, Any]]:
    target = str(reference or "").strip()
    if not target.startswith(WORKSPACE_SKILL_PREFIX):
        return None
    for item in list_workspace_skills(int(user_id)):
        if item.get("slug") == target:
            return item
    return None


def read_workspace_skill(
    user_id: int,
    reference: str,
    *,
    ai_config_id: Optional[int] = None,
) -> Dict[str, Any]:
    item = find_workspace_skill(int(user_id), reference)
    if item is None:
        raise ValueError("workspace Skill not found")
    if (
        ai_config_id is not None
        and item.get("scope") != "global"
        and str(item.get("owner_ai_config_id")) != str(int(ai_config_id))
    ):
        raise PermissionError("workspace Skill is not available to this AI")
    try:
        with open(str(item["_absolute_path"]), "r", encoding="utf-8") as handle:
            raw = handle.read(MAX_WORKSPACE_SKILL_CARD_BYTES + 1)
    except (OSError, UnicodeError) as exc:
        raise ValueError("workspace Skill is unreadable") from exc
    if len(raw.encode("utf-8")) > MAX_WORKSPACE_SKILL_CARD_BYTES:
        raise ValueError("workspace Skill is too large")
    _, body = _split_frontmatter(raw)
    body = body.strip() + "\n" if body.strip() else ""
    public_item = {key: value for key, value in item.items() if not key.startswith("_")}
    return {
        "slug": item["slug"],
        "skill": public_item,
        "skill_card": body,
        "metadata": {
            "source": "workspace",
            "version": item.get("version"),
            "owner_ai_config_id": item.get("owner_ai_config_id"),
            "scope": item.get("scope"),
        },
        "path": item["path"],
        "present": True,
        "lines": [
            {"line": index, "text": line}
            for index, line in enumerate(body.splitlines(), start=1)
        ],
        "line_count": len(body.splitlines()),
    }


def workspace_entry_body(entry: Dict[str, Any]) -> Optional[str]:
    path = entry.get("_absolute_path")
    if not path:
        return None
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            _, body = _split_frontmatter(handle.read(MAX_WORKSPACE_SKILL_CARD_BYTES + 1))
        return body
    except (OSError, UnicodeError):
        return ""


def _replace_workspace_card_body(raw: str, body: str) -> str:
    normalized = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
    if normalized.startswith("---\n"):
        end = normalized.find("\n---\n", 4)
        if end >= 0:
            header = normalized[: end + 5]
            return header + "\n" + str(body or "").strip() + "\n"
    return str(body or "").strip() + "\n"


def update_workspace_skill_content(
    user_id: int,
    reference: str,
    body: str,
) -> Dict[str, Any]:
    """Update only the body, preserving the AI-authored frontmatter."""
    item = find_workspace_skill(int(user_id), reference)
    if item is None:
        raise ValueError("workspace Skill not found")
    content = str(body or "").strip()
    if not content:
        raise ValueError("workspace Skill content is required")
    with open(str(item["_absolute_path"]), "r", encoding="utf-8") as handle:
        raw = handle.read(MAX_WORKSPACE_SKILL_CARD_BYTES + 1)
    if len(raw.encode("utf-8")) > MAX_WORKSPACE_SKILL_CARD_BYTES:
        raise ValueError("workspace Skill is too large")
    updated = _replace_workspace_card_body(raw, content)
    if len(updated.encode("utf-8")) > MAX_WORKSPACE_SKILL_CARD_BYTES:
        raise ValueError("workspace Skill is too large")
    path = str(item["_absolute_path"])
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(updated)
    os.replace(temporary, path)
    return read_workspace_skill(int(user_id), reference)


def update_workspace_skill_endpoint(
    user_id: int,
    reference: str,
    endpoint_kind: Any,
) -> Dict[str, Any]:
    """Update the optional endpoint hint without changing the Skill body."""
    item = find_workspace_skill(int(user_id), reference)
    if item is None:
        raise ValueError("workspace Skill not found")
    with open(str(item["_absolute_path"]), "r", encoding="utf-8") as handle:
        raw = handle.read(MAX_WORKSPACE_SKILL_CARD_BYTES + 1)
    if len(raw.encode("utf-8")) > MAX_WORKSPACE_SKILL_CARD_BYTES:
        raise ValueError("workspace Skill is too large")
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    kind = _normalize_endpoint(endpoint_kind)
    if normalized.startswith("---\n"):
        end = normalized.find("\n---\n", 4)
        if end >= 0:
            header = normalized[4:end]
            lines = header.splitlines()
            replaced = False
            for index, line in enumerate(lines):
                if line.split(":", 1)[0].strip() == "endpoint_kind":
                    lines[index] = f"endpoint_kind: {json.dumps(kind, ensure_ascii=False)}"
                    replaced = True
                    break
            if not replaced:
                lines.append(f"endpoint_kind: {json.dumps(kind, ensure_ascii=False)}")
            normalized = "---\n" + "\n".join(lines) + "\n---\n" + normalized[end + 5 :]
    else:
        normalized = (
            "---\n"
            f"name: {json.dumps(item['displayName'], ensure_ascii=False)}\n"
            f"endpoint_kind: {json.dumps(kind, ensure_ascii=False)}\n"
            "---\n\n"
            + normalized
        )
    path = str(item["_absolute_path"])
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(normalized)
    os.replace(temporary, path)
    return read_workspace_skill(int(user_id), reference)


def delete_workspace_skill(user_id: int, reference: str) -> Dict[str, Any]:
    """Remove the Skill card while preserving any user-owned resources beside it."""
    item = find_workspace_skill(int(user_id), reference)
    if item is None:
        raise ValueError("workspace Skill not found")
    os.remove(str(item["_absolute_path"]))
    return {"deleted": True, "slug": reference}
