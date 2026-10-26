"""Curated visibility policy for AI workspace discovery.

The workspace is also used for runtime state and generated artifacts.  A raw
recursive directory listing therefore produces noisy, misleading context (and
can expose local configuration names).  This module is deliberately read-only
and shared by the gateway, MCP runtime, and workspace tools so every discovery
surface uses the same defaults.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterator

from fastapi import HTTPException


IGNORED_DIRECTORY_NAMES = frozenset({
    ".aider", ".cache", ".git", ".heysure", ".mypy_cache", ".nox", ".pytest_cache",
    ".ruff_cache", ".sandbox_home", ".sandbox_tmp", ".tox", ".venv", "__pycache__",
    "_admins", "backups", "build", "coverage", "dist", "htmlcov", "logs", "node_modules",
    "postgres", "target", "temp", "tmp", "venv",
})
HIDDEN_FILE_NAMES = frozenset({".env", ".env.local", ".env.production", ".env.development"})
IGNORED_FILE_SUFFIXES = frozenset({
    ".bak", ".db", ".log", ".pyc", ".pyo", ".sqlite", ".sqlite3", ".tmp", ".swp",
})
DEFAULT_MAX_DEPTH = 8
DEFAULT_MAX_ENTRIES = 300
MAX_MAX_DEPTH = 32
MAX_MAX_ENTRIES = 2000


def safe_workspace_path(root: str, rel: str) -> str:
    root_real = os.path.realpath(root)
    clean = str(rel or "").strip().replace("\\", "/").lstrip("/")
    full = os.path.realpath(os.path.join(root_real, clean))
    if full != root_real and not full.startswith(root_real + os.sep):
        raise HTTPException(status_code=400, detail="非法的文件路径")
    return full


def workspace_rel_path(root: str, full: str) -> str:
    rel = os.path.relpath(full, os.path.realpath(root)).replace(os.sep, "/")
    return "" if rel == "." else rel


def _part_is_hidden(name: str) -> bool:
    return name.startswith(".") or name in HIDDEN_FILE_NAMES


def should_skip_directory(name: str, *, include_generated: bool = False, include_hidden: bool = False) -> bool:
    """Return whether a directory is hidden from the curated view."""
    if include_generated and include_hidden:
        return False
    lowered = str(name or "").lower()
    if not include_generated and lowered in IGNORED_DIRECTORY_NAMES:
        return True
    return not include_hidden and _part_is_hidden(name)


def should_skip_file(name: str, *, include_generated: bool = False, include_hidden: bool = False) -> bool:
    """Return whether a file is hidden from the curated view."""
    if not include_generated and _part_is_hidden(name):
        return True
    if not include_generated and str(name).lower().endswith(tuple(IGNORED_FILE_SUFFIXES)):
        return True
    return False


def _bounded(value: object, default: int, maximum: int) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


def _matches_query(relative_path: str, query: str) -> bool:
    value = str(query or "").strip().lower()
    return not value or value in relative_path.lower()


def _children(directory: Path) -> list[Path]:
    try:
        return sorted(directory.iterdir(), key=lambda item: item.name.lower())
    except OSError:
        return []


def _walk_entries(
    directory: Path,
    root_path: Path,
    depth: int,
    depth_limit: int,
    entry_limit: int,
    query: str,
    policy: Dict[str, bool],
    state: Dict[str, int],
) -> Iterator[Dict[str, object]]:
    if depth > depth_limit or state["emitted"] >= entry_limit:
        return
    for child in _children(directory):
        if state["emitted"] >= entry_limit or child.is_symlink():
            return
        relative = child.relative_to(root_path).as_posix()
        if child.is_dir():
            if should_skip_directory(child.name, **policy):
                continue
            if _matches_query(relative, query):
                state["emitted"] += 1
                yield {"path": relative, "kind": "directory"}
            yield from _walk_entries(
                child, root_path, depth + 1, depth_limit, entry_limit, query,
                policy, state,
            )
            continue
        if not child.is_file() or should_skip_file(child.name, **policy) or not _matches_query(relative, query):
            continue
        try:
            size = child.stat().st_size
        except OSError:
            continue
        state["emitted"] += 1
        yield {"path": relative, "kind": "file", "bytes": size}


def iter_workspace_entries(
    root: str,
    *,
    path: str = "",
    query: str = "",
    include_generated: bool = False,
    include_hidden: bool = False,
    max_depth: object = DEFAULT_MAX_DEPTH,
    max_entries: object = DEFAULT_MAX_ENTRIES,
) -> Iterator[Dict[str, object]]:
    """Yield bounded, relative workspace entries in deterministic order."""
    root_path = Path(root).resolve()
    start = (root_path / str(path or "").replace("\\", "/")).resolve()
    if start != root_path and root_path not in start.parents:
        return
    if not start.is_dir():
        return
    state = {"emitted": 0}
    yield from _walk_entries(
        start, root_path, 0, _bounded(max_depth, DEFAULT_MAX_DEPTH, MAX_MAX_DEPTH),
        _bounded(max_entries, DEFAULT_MAX_ENTRIES, MAX_MAX_ENTRIES), str(query or "").strip().lower(),
        {"include_generated": include_generated, "include_hidden": include_hidden}, state,
    )


def list_workspace_entries(root: str, **kwargs: object) -> Dict[str, object]:
    """Return the curated listing plus truncation metadata."""
    entries = list(iter_workspace_entries(root, **kwargs))
    max_entries = _bounded(kwargs.get("max_entries"), DEFAULT_MAX_ENTRIES, MAX_MAX_ENTRIES)
    return {
        "entries": entries,
        "count": len(entries),
        "truncated": len(entries) >= max_entries,
        "policy": "curated",
    }


def workspace_listing_paths(root: str, **kwargs: object) -> list[str]:
    """Adapt curated entries to the legacy flat path API."""
    listing = list_workspace_entries(root, **kwargs)
    return [
        f"{entry['path']}/" if entry.get("kind") == "directory" else str(entry["path"])
        for entry in listing["entries"]
    ]


def workspace_tree(root: str, **kwargs: object) -> str:
    """Render a compact tree from the same policy used by curated listings."""
    listing = list_workspace_entries(root, **kwargs)
    rows = listing["entries"]
    lines = []
    for item in rows:
        entry = item if isinstance(item, dict) else {}
        marker = "[dir] " if entry.get("kind") == "directory" else "      "
        lines.append(f"{marker}{entry.get('path', '')}")
    if listing.get("truncated"):
        lines.append("… listing truncated; use a narrower path/query or include_generated=true")
    return "\n".join(lines) + ("\n" if lines else "")
