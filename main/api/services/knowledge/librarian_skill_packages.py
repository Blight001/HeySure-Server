from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from .librarian_core import (
    _normalize_endpoint,
    _read_text,
    _resolve_endpoint_kind,
    _split_frontmatter,
    _upsert_thought,
)
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

    # 单文件 .md 模型：只取技能卡 SKILL.md 内容；其余附属文件不再随技能落盘。
    card_path = os.path.join(source_dir, "SKILL.md")
    if not os.path.isfile(card_path):
        raise ValueError(f"installed skill has no SKILL.md: {skill_name}")
    raw = _read_text(card_path) or ""
    fm, stripped = _split_frontmatter(raw)
    body_text = stripped if fm else raw
    name = str((fm or {}).get("name") or skill_name).strip() or skill_name
    description = str((fm or {}).get("description") or "").strip()

    row = {
        "slug": thought_id,
        "displayName": name,
        "summary": description,
        "version": None,
        "ownerHandle": "",
        "source": "npx:skills",
        "installed_at": time.time(),
        "auto_enabled": False,
        "endpoint_kind": _normalize_endpoint(endpoint_kind),
        "trust": {"verdict": "unverified"},
    }
    merged = _upsert_thought(int(user_id), row, body=body_text)
    return dict(merged, id=thought_id)


def _run_skill_install(npx: str, package: str, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [npx, "skills", "add", package, "-g", "-y"], shell=False, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"npx skills install timed out after {timeout_seconds} seconds") from exc
    except OSError as exc:
        raise ValueError(f"failed to start npx skills: {exc}") from exc


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
    result = _run_skill_install(npx, package, timeout_seconds)
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part and part.strip())
    if result.returncode != 0:
        raise ValueError(f"npx skills install failed ({result.returncode}): {output[-4000:]}")

    after = _global_skill_snapshot(global_root)
    lock_after = _global_skills_lock_snapshot()
    changed = [name for name, value in lock_after.items() if name in after and lock_before.get(name) != value]
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


