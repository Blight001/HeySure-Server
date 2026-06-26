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
    _INHERITANCE_THOUGHTS_DIR,
    _CLAWHUB_REMOTE_DIR,
    _NPX_SKILLS_DIR,
    _MANUAL_SKILLS_DIR,
    _normalize_triggers,
    _parse_triggers_field,
    _safe_write,
    _topic_path,
    _read_text,
    _load_clawhub_state,
    _save_clawhub_state,
    _clawhub_installed_items,
    _entry_dict_from_file_entry,
)
from ..integrations import clawhub
from .knowledge_vector import sync_topic_embedding_for_entry as _sync_topic_embedding

logger = logging.getLogger(__name__)


def _inheritance_thoughts_payload(user_id: int) -> Dict[str, Any]:
    installed = _clawhub_installed_items(user_id)
    return {
        "description": "传承思想支持从 ClawHub 或 npx skills 安装 Skill 到本地 KnowledgeBase 快照；运行时只使用本地文件。",
        "registry_url": clawhub.registry_base_url(),
        "storage_root": f"{_INHERITANCE_THOUGHTS_DIR}/{_CLAWHUB_REMOTE_DIR}",
        "installed_total": len(installed),
        "installed": installed,
    }


def list_inheritance_thoughts(*, user_id: int) -> Dict[str, Any]:
    """Return installed inheritance thoughts using their ClawHub slug as ID."""
    payload = _inheritance_thoughts_payload(int(user_id))
    items: List[Dict[str, Any]] = []
    for installed in payload.get("installed") or []:
        item = dict(installed)
        item["id"] = str(item.get("slug") or "")
        items.append(item)
    return {
        "items": items,
        "total": len(items),
        "description": payload.get("description"),
        "storage_root": payload.get("storage_root"),
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
    """按行编辑 SKILL.md 与/或改端（endpoint_kind）。两者均可单独使用。"""
    from .librarian_clawhub import (
        clawhub_installed_skill_detail,
        update_clawhub_installed_skill,
        _clawhub_installed_dir,
        _normalize_clawhub_slug,
    )
    thought_id = _normalize_clawhub_slug(thought_id)
    endpoint_raw = arguments.get("endpoint_kind")
    has_endpoint = endpoint_raw is not None and str(endpoint_raw).strip() != ""
    has_line_edits = _has_skill_line_edits(arguments)
    if not has_line_edits and not has_endpoint:
        raise ValueError("nothing to edit: provide line edits and/or endpoint_kind")

    current = clawhub_installed_skill_detail(user_id=int(user_id), slug=thought_id)
    old_content = str(current.get("skill_card") or "")
    old_sha256 = _text_sha256(old_content)
    new_content = old_content
    edit_count = 0

    if has_line_edits:
        expected = str(arguments.get("expected_sha256") or "").strip().lower()
        if expected and expected != old_sha256:
            raise ValueError("SKILL.md changed after it was read; read it again before editing")
        new_content, edit_count = _apply_skill_line_edits(old_content, arguments)
        update_clawhub_installed_skill(
            user_id=int(user_id),
            slug=thought_id,
            skill_card=new_content,
        )

    state = _load_clawhub_state(int(user_id))
    installed = state.get("installed") if isinstance(state.get("installed"), dict) else {}
    row = installed.get(thought_id)
    if isinstance(row, dict):
        if has_line_edits:
            install_dir = _clawhub_installed_dir(int(user_id), row)
            metadata = _skill_card_metadata(install_dir, str(row.get("displayName") or thought_id))
            row["displayName"] = metadata["name"]
            row["summary"] = metadata["description"]
        if has_endpoint:
            row["endpoint_kind"] = _normalize_endpoint(endpoint_raw)
        installed[thought_id] = row
        state["installed"] = installed
        _save_clawhub_state(int(user_id), state)

    final_endpoint = _normalize_endpoint(
        row.get("endpoint_kind") if isinstance(row, dict) else endpoint_raw
    )
    return {
        "updated": True,
        "id": thought_id,
        "edit_count": edit_count,
        "endpoint_kind": final_endpoint,
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

    try:
        _sync_topic_embedding(user_id=user_id, row=entry_dict, ai_config_id=ai_config_id, force=True)
    except Exception as exc:
        logger.info("skill file-embedding sync failed slug=%s: %s", slug, exc)

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
    safe_name = thought_id.split("/", 1)[1]
    install_rel = f"{_INHERITANCE_THOUGHTS_DIR}/{_MANUAL_SKILLS_DIR}/{safe_name}"
    install_dir = safe_join(_kb_root(user_id), install_rel)
    os.makedirs(install_dir, exist_ok=True)

    summary_text = str(summary or "").strip()
    skill_md = _ensure_skill_frontmatter(body, name=name, description=summary_text)
    _safe_write(os.path.join(install_dir, "SKILL.md"), skill_md)
    installed_at = time.time()
    with open(os.path.join(install_dir, "heysure_manual_create.json"), "w", encoding="utf-8") as f:
        json.dump(
            {"source": "manual", "name": name, "installed_at": installed_at},
            f,
            ensure_ascii=False,
            indent=2,
        )

    meta = _skill_card_metadata(install_dir, name)
    resolved_endpoint = _resolve_endpoint_kind(int(user_id), ai_config_id, endpoint_kind)
    state = _load_clawhub_state(user_id)
    installed = state.get("installed") if isinstance(state.get("installed"), dict) else {}
    installed[thought_id] = {
        "slug": thought_id,
        "displayName": meta["name"],
        "summary": meta["description"] or summary_text,
        "version": None,
        "ownerHandle": "",
        "source": "manual",
        "path": install_rel,
        "installed_at": installed_at,
        "auto_enabled": False,
        "endpoint_kind": resolved_endpoint,
        "trust": {"verdict": "self-authored"},
    }
    state["installed"] = installed
    _save_clawhub_state(user_id, state)
    try:
        _sync_skill_to_knowledge_entry(
            user_id=user_id,
            slug=thought_id,
            name=meta["name"],
            summary=meta["description"] or summary_text,
            skill_md_path=install_rel.rstrip("/\\") + "/SKILL.md",
            installed_at=installed_at,
            ai_config_id=ai_config_id,
            status="active",
        )
    except Exception as exc:
        logger.warning("skill sync to knowledge entry failed slug=%s: %s", thought_id, exc)
    return dict(installed[thought_id], id=thought_id)


def delete_inheritance_thought(*, user_id: int, thought_id: str) -> Dict[str, Any]:
    from .librarian_clawhub import delete_clawhub_installed_skill
    result = delete_clawhub_installed_skill(
        user_id=int(user_id),
        slug=str(thought_id or "").strip(),
    )
    return {"deleted": True, "id": str(result.get("slug") or thought_id)}


_SAFE_SKILLS_PACKAGE = re.compile(r"^[^\x00-\x1f\x7f]{1,500}$")


def _normalize_skills_package(package: str) -> str:
    value = str(package or "").strip()
    lowered = value.lower()
    if (
        not value
        or value.startswith(("-", ".", "/", "\\"))
        or re.match(r"^[a-zA-Z]:[\\/]", value)
        or lowered.startswith("file:")
        or not _SAFE_SKILLS_PACKAGE.fullmatch(value)
    ):
        raise ValueError("invalid skills package")
    return value


def _global_agent_skills_root() -> str:
    return os.path.join(str(Path.home()), ".agents", "skills")


def _skill_directory_fingerprint(path: str) -> str:
    import hashlib

    digest = hashlib.sha256()
    root = os.path.abspath(path)
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        files.sort()
        for name in dirs:
            item = os.path.join(current, name)
            if os.path.islink(item):
                rel = os.path.relpath(item, root).replace("\\", "/")
                digest.update(f"link:{rel}\0{os.readlink(item)}".encode("utf-8"))
        for name in files:
            item = os.path.join(current, name)
            if os.path.islink(item):
                rel = os.path.relpath(item, root).replace("\\", "/")
                digest.update(f"link:{rel}\0{os.readlink(item)}".encode("utf-8"))
                continue
            rel = os.path.relpath(item, root).replace("\\", "/")
            digest.update(f"{rel}\0".encode("utf-8"))
            with open(item, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


def _global_skill_snapshot(root: str) -> Dict[str, str]:
    if not os.path.isdir(root):
        return {}
    snapshot: Dict[str, str] = {}
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if os.path.isdir(path) and os.path.isfile(os.path.join(path, "SKILL.md")):
            snapshot[name] = _skill_directory_fingerprint(path)
    return snapshot


def _global_skills_lock_path() -> str:
    state_home = str(os.environ.get("XDG_STATE_HOME") or "").strip()
    if state_home:
        return os.path.join(state_home, "skills", ".skill-lock.json")
    return os.path.join(str(Path.home()), ".agents", ".skill-lock.json")


def _global_skills_lock_snapshot() -> Dict[str, str]:
    try:
        with open(_global_skills_lock_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    skills = data.get("skills") if isinstance(data, dict) else None
    if not isinstance(skills, dict):
        return {}
    return {
        str(name): json.dumps(item, ensure_ascii=False, sort_keys=True)
        for name, item in skills.items()
        if isinstance(item, dict)
    }


def _skill_card_metadata(skill_dir: str, fallback_name: str) -> Dict[str, str]:
    card_path = os.path.join(skill_dir, "SKILL.md")
    with open(card_path, "r", encoding="utf-8") as f:
        card = f.read()
    metadata: Dict[str, Any] = {}
    if card.startswith("---"):
        end = card.find("\n---", 3)
        if end >= 0:
            try:
                loaded = yaml.safe_load(card[3:end])
                metadata = loaded if isinstance(loaded, dict) else {}
            except Exception:
                metadata = {}
    return {
        "name": str(metadata.get("name") or fallback_name).strip() or fallback_name,
        "description": str(metadata.get("description") or "").strip(),
    }


def _validate_skill_tree_for_import(skill_dir: str) -> None:
    if os.path.islink(skill_dir):
        raise ValueError("global skill directory may not be a symlink")
    for current, dirs, files in os.walk(skill_dir, followlinks=False):
        for name in dirs + files:
            if os.path.islink(os.path.join(current, name)):
                raise ValueError(f"skill contains unsupported symlink: {name}")


def _import_global_skill_snapshot(
    *,
    user_id: int,
    package: str,
    skill_name: str,
    source_dir: str,
    endpoint_kind: str = "any",
    ai_config_id: Optional[int] = None,
) -> Dict[str, Any]:
    thought_id = f"npx/{skill_name}"
    _validate_skill_tree_for_import(source_dir)
    import hashlib

    safe_name = f"{_slugify(skill_name)}-{hashlib.sha1(skill_name.encode('utf-8')).hexdigest()[:8]}"
    install_rel = f"{_INHERITANCE_THOUGHTS_DIR}/{_NPX_SKILLS_DIR}/{safe_name}"
    install_dir = safe_join(_kb_root(user_id), install_rel)
    temp_dir = f"{install_dir}.tmp-{uuid.uuid4().hex[:8]}"
    os.makedirs(os.path.dirname(install_dir), exist_ok=True)
    try:
        shutil.copytree(source_dir, temp_dir)
        if not os.path.isfile(os.path.join(temp_dir, "SKILL.md")):
            raise ValueError(f"installed skill has no SKILL.md: {skill_name}")
        if os.path.isdir(install_dir):
            shutil.rmtree(install_dir)
        os.replace(temp_dir, install_dir)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    meta = _skill_card_metadata(install_dir, skill_name)
    installed_at = time.time()
    install_meta = {
        "source": "npx:skills",
        "package": package,
        "skill_name": skill_name,
        "installed_at": installed_at,
        "global_source_path": source_dir,
    }
    with open(os.path.join(install_dir, "heysure_npx_install.json"), "w", encoding="utf-8") as f:
        json.dump(install_meta, f, ensure_ascii=False, indent=2)

    state = _load_clawhub_state(user_id)
    installed = state.get("installed") if isinstance(state.get("installed"), dict) else {}
    installed[thought_id] = {
        "slug": thought_id,
        "displayName": meta["name"],
        "summary": meta["description"],
        "version": None,
        "ownerHandle": "",
        "source": "npx:skills",
        "package": package,
        "path": install_rel,
        "installed_at": installed_at,
        "auto_enabled": False,
        "endpoint_kind": _normalize_endpoint(endpoint_kind),
        "trust": {"verdict": "unverified"},
    }
    state["installed"] = installed
    _save_clawhub_state(user_id, state)
    try:
        _sync_skill_to_knowledge_entry(
            user_id=user_id,
            slug=thought_id,
            name=meta["name"],
            summary=meta["description"],
            skill_md_path=install_rel.rstrip("/\\") + "/SKILL.md",
            installed_at=installed_at,
            ai_config_id=ai_config_id,
            status="active",
        )
    except Exception as exc:
        logger.warning("skill sync to knowledge entry failed slug=%s: %s", thought_id, exc)
    return dict(installed[thought_id], id=thought_id)


def install_npx_skill_package(
    *,
    user_id: int,
    package: str,
    timeout: Optional[int] = None,
    endpoint_kind: Optional[str] = None,
    ai_config_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Install via skills CLI, then snapshot changed global skills into the KB.

    ``endpoint_kind`` 端归类（any/desktop/browser）：显式传入优先，未传则按安装
    成员当前绑定的端侧 agent 自动推断。
    """
    package = _normalize_skills_package(package)
    resolved_endpoint = _resolve_endpoint_kind(int(user_id), ai_config_id, endpoint_kind)
    try:
        timeout_seconds = int(timeout or 300)
    except (TypeError, ValueError):
        timeout_seconds = 300
    timeout_seconds = max(30, min(timeout_seconds, 600))
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    if not npx:
        raise ValueError("npx is not installed or not available in PATH")

    global_root = _global_agent_skills_root()
    before = _global_skill_snapshot(global_root)
    lock_before = _global_skills_lock_snapshot()
    try:
        result = subprocess.run(
            [npx, "skills", "add", package, "-g", "-y"],
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"npx skills install timed out after {timeout_seconds} seconds") from exc
    except OSError as exc:
        raise ValueError(f"failed to start npx skills: {exc}") from exc
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part and part.strip())
    if result.returncode != 0:
        raise ValueError(f"npx skills install failed ({result.returncode}): {output[-4000:]}")

    after = _global_skill_snapshot(global_root)
    lock_after = _global_skills_lock_snapshot()
    changed = [name for name, value in lock_after.items() if lock_before.get(name) != value and name in after]
    if not changed:
        changed = [name for name, fingerprint in after.items() if before.get(name) != fingerprint]
    if not changed:
        raise ValueError("installation succeeded but no new or updated global skills were detected")

    imported = [
        _import_global_skill_snapshot(
            user_id=int(user_id),
            package=package,
            skill_name=name,
            source_dir=os.path.join(global_root, name),
            endpoint_kind=resolved_endpoint,
            ai_config_id=ai_config_id,
        )
        for name in changed
    ]
    return {
        "installed": True,
        "package": package,
        "command": "npx skills add <package> -g -y",
        "imported": imported,
        "total": len(imported),
        "output": output[-8000:],
    }


def _render_inheritance_thoughts_body(payload: Dict[str, Any]) -> str:
    lines = [
        "# 传承思想",
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
        lines.append("## 已安装 ClawHub 技能")
        lines.append("")
        for item in installed:
            slug = str(item.get("slug") or "")
            name = str(item.get("displayName") or slug)
            version = str(item.get("version") or "latest")
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