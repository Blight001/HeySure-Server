#!/usr/bin/env python3
"""Host-side updater for container deployments.

The API gateway runs inside Docker and often does not have the host Git
workspace mounted. This tiny HTTP service runs on the host, receives requests
from the gateway, and performs the real deployment update in the workspace:

    git fetch -> git pull --ff-only -> git submodule update -> docker compose up

Bind it to 127.0.0.1 and expose it to containers through host.docker.internal.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("HEYSURE_REPO_ROOT") or Path(__file__).resolve().parents[3]).resolve()
HOST = os.environ.get("HEYSURE_REPO_UPDATER_HOST", "127.0.0.1")
PORT = int(os.environ.get("HEYSURE_REPO_UPDATER_PORT", "58151"))
TOKEN = os.environ.get("HEYSURE_REPO_UPDATER_TOKEN") or os.environ.get("HEYSURE_INTERNAL_TOKEN") or ""
COMPOSE_CMD = shlex.split(os.environ.get("HEYSURE_REPO_UPDATER_COMPOSE_CMD", "docker compose"))
VERSION_FILE = ROOT / "server" / "data" / "deployed-version.json"

_lock = threading.Lock()
_state: dict[str, Any] = {
    "running": False,
    "phase": "idle",
    "message": "",
    "last_error": "",
    "updated_at": time.time(),
}


class UpdateError(RuntimeError):
    pass


def _set_state(**fields: Any) -> None:
    _state.update(fields)
    _state["updated_at"] = time.time()


def _run(cmd: list[str], timeout: float = 300.0) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise UpdateError(f"{' '.join(cmd)} failed: {detail}")
    return proc


def _git(args: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return _run(["git", *args], timeout=timeout)


def _branch() -> str:
    proc = _git(["rev-parse", "--abbrev-ref", "HEAD"], timeout=15)
    branch = proc.stdout.strip()
    return branch if branch and branch != "HEAD" else ""


def _commit_info(ref: str = "HEAD") -> dict[str, Any] | None:
    proc = _git(["log", "-1", "--format=%H%n%h%n%an%n%ct%n%s", ref], timeout=15)
    parts = proc.stdout.strip().split("\n", 4)
    if len(parts) < 5:
        return None
    sha, short, author, ts, subject = parts
    try:
        committed_at: float | None = float(ts)
    except ValueError:
        committed_at = None
    body = _git(["show", "-s", "--format=%B", ref], timeout=15).stdout.strip() or subject
    files: list[dict[str, Any]] = []
    stat = _git(["show", "--format=", "--numstat", ref], timeout=30)
    for line in stat.stdout.splitlines()[:200]:
        cols = line.split("\t", 2)
        if len(cols) != 3:
            continue
        added, deleted, path = cols
        files.append({
            "path": path,
            "added": None if added == "-" else int(added),
            "deleted": None if deleted == "-" else int(deleted),
        })
    return {
        "sha": sha,
        "short": short,
        "author": author,
        "committed_at": committed_at,
        "subject": subject,
        "body": body,
        "files": files,
    }


def _version() -> dict[str, Any]:
    return {"git_available": True, "branch": _branch(), "current": _commit_info("HEAD")}


def _compare() -> dict[str, Any]:
    branch = _branch()
    fetch_args = ["fetch", "--quiet", "origin", branch] if branch else ["fetch", "--quiet", "origin"]
    _git(fetch_args, timeout=180)
    upstream = f"origin/{branch}" if branch else ""
    if not upstream:
        proc = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], timeout=15)
        upstream = proc.stdout.strip()
    if not upstream:
        raise UpdateError("cannot determine upstream branch")
    counts = _git(["rev-list", "--left-right", "--count", f"HEAD...{upstream}"], timeout=30)
    left, _, right = counts.stdout.strip().replace("\t", " ").partition(" ")
    ahead = int(left or "0")
    behind = int(right or "0")
    return {
        "branch": branch,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "current": _commit_info("HEAD"),
        "remote": _commit_info(upstream),
    }


def _write_version_file(payload: dict[str, Any]) -> None:
    VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = VERSION_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(VERSION_FILE)


def _compose_rebuild() -> None:
    try:
        _set_state(phase="rebuilding", message="building docker images")
        _run([*COMPOSE_CMD, "build", "--progress", "plain"], timeout=1800)
        _set_state(phase="restarting", message="recreating compose services")
        _run([*COMPOSE_CMD, "up", "-d", "--remove-orphans"], timeout=900)
        _set_state(running=False, phase="done", message="services updated", last_error="")
    except Exception as exc:
        _set_state(running=False, phase="error", message="update failed", last_error=str(exc))


def _check_and_update(apply: bool) -> dict[str, Any]:
    if not _lock.acquire(blocking=False):
        return {"ok": False, "busy": True, "state": dict(_state)}
    try:
        _set_state(running=True, phase="checking", message="checking remote updates", last_error="")
        info = _compare()
        if info["behind"] <= 0:
            _set_state(running=False, phase="up_to_date", message="already up to date")
            return {"ok": True, "updated": False, "update_available": False, **info, "state": dict(_state)}
        if not apply:
            _set_state(running=False, phase="update_available", message=f"{info['behind']} commits available")
            return {"ok": True, "updated": False, "update_available": True, **info, "state": dict(_state)}

        from_sha = ((info.get("current") or {}).get("sha") or "")
        _set_state(phase="pulling", message="pulling latest code")
        _git(["pull", "--ff-only", "origin", info["branch"]] if info["branch"] else ["pull", "--ff-only"], timeout=300)
        _git(["submodule", "update", "--init", "--recursive"], timeout=300)
        version = _version()
        to_sha = ((version.get("current") or {}).get("sha") or "")
        _write_version_file(version)
        _set_state(phase="queued_restart", message="compose rebuild queued")
        threading.Thread(target=_compose_rebuild, name="heysure-compose-rebuild", daemon=True).start()
        return {
            "ok": True,
            "updated": True,
            "restarting": True,
            "from": from_sha,
            "to": to_sha,
            **info,
            "current": version.get("current"),
            "state": dict(_state),
        }
    except Exception as exc:
        _set_state(running=False, phase="error", message="update failed", last_error=str(exc))
        return {"ok": False, "error": str(exc), "state": dict(_state)}
    finally:
        _lock.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "HeySureRepoUpdater/1.0"

    def _authorized(self) -> bool:
        if not TOKEN:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {TOKEN}"

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"ok": True, "root": str(ROOT), "state": dict(_state)})
            return
        if not self._authorized():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            if self.path == "/version":
                self._json(200, _version())
            elif self.path == "/state":
                self._json(200, {"ok": True, "state": dict(_state)})
            else:
                self._json(404, {"ok": False, "error": "not found"})
        except Exception as exc:
            self._json(500, {"ok": False, "error": str(exc)})

    def do_POST(self) -> None:
        if not self._authorized():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            payload = {}
        if self.path != "/check":
            self._json(404, {"ok": False, "error": "not found"})
            return
        self._json(200, _check_and_update(bool(payload.get("apply", True))))

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {self.address_string()} {fmt % args}", flush=True)


def main() -> None:
    print(f"HeySure repo updater listening on http://{HOST}:{PORT} (root={ROOT})", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
