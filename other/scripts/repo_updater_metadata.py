"""Repository/deployed-version metadata helpers for the host updater."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable


GitOutput = Callable[[list[str], float], str]


def branch(git_output: GitOutput) -> str:
    value = git_output(["rev-parse", "--abbrev-ref", "HEAD"], 15)
    return value if value and value != "HEAD" else ""


def commit_info(git_output: GitOutput, ref: str = "HEAD") -> dict[str, Any] | None:
    parts = git_output(["log", "-1", "--format=%H%n%h%n%an%n%ct%n%s", ref], 15).split(
        "\n", 4
    )
    if len(parts) < 5:
        return None
    sha, short, author, timestamp, subject = parts
    try:
        committed_at: float | None = float(timestamp)
    except ValueError:
        committed_at = None
    body = git_output(["show", "-s", "--format=%B", ref], 15) or subject
    files: list[dict[str, Any]] = []
    for line in git_output(["show", "--format=", "--numstat", ref], 30).splitlines()[:200]:
        added, separator, remainder = line.partition("\t")
        deleted, separator2, path = remainder.partition("\t")
        if not separator or not separator2:
            continue
        files.append(
            {
                "path": path,
                "added": None if added == "-" else int(added),
                "deleted": None if deleted == "-" else int(deleted),
            }
        )
    return {
        "sha": sha,
        "short": short,
        "author": author,
        "committed_at": committed_at,
        "subject": subject,
        "body": body,
        "files": files,
    }


def read_deployed_version(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("current"), dict):
        return None
    payload.setdefault("git_available", False)
    payload.setdefault("branch", "")
    return payload


def write_deployed_version(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def deployment_snapshot(git_output: GitOutput) -> dict[str, Any]:
    return {
        "git_available": True,
        "branch": branch(git_output),
        "current": commit_info(git_output, "HEAD"),
    }


def live_version(git_output: GitOutput, deployed_path: Path) -> dict[str, Any]:
    try:
        payload = deployment_snapshot(git_output)
        if payload["current"]:
            cached = read_deployed_version(deployed_path)
            payload["deployed_current"] = (cached or {}).get("current")
            payload["deployment_pending"] = bool(
                cached
                and ((cached.get("current") or {}).get("sha") != payload["current"].get("sha"))
            )
            return payload
    except Exception:
        cached = read_deployed_version(deployed_path)
        if cached is not None:
            return cached
        raise
    cached = read_deployed_version(deployed_path)
    return cached or {"git_available": False, "branch": "", "current": None}


def compare_remote(git_output: GitOutput) -> dict[str, Any]:
    current_branch = branch(git_output)
    fetch = ["fetch", "--quiet", "origin", current_branch] if current_branch else [
        "fetch", "--quiet", "origin"
    ]
    git_output(fetch, 180)
    upstream = f"origin/{current_branch}" if current_branch else ""
    if not upstream:
        upstream = git_output(
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], 15
        )
    if not upstream:
        raise RuntimeError("cannot determine upstream branch")
    counts = git_output(["rev-list", "--left-right", "--count", f"HEAD...{upstream}"], 30)
    left, _, right = counts.replace("\t", " ").partition(" ")
    return {
        "branch": current_branch,
        "upstream": upstream,
        "ahead": int(left or "0"),
        "behind": int(right or "0"),
        "current": commit_info(git_output, "HEAD"),
        "remote": commit_info(git_output, upstream),
    }
