#!/usr/bin/env python3
"""Host-side updater for container deployments.

The API gateway runs inside Docker and often does not have the host Git
workspace mounted. This tiny HTTP service runs on the host, receives requests
from the gateway, and performs the real deployment update in the workspace:

    git fetch -> git pull --ff-only -> git submodule update -> docker compose up

Bind it to 0.0.0.0 and expose it to containers through host.docker.internal.
"""

from __future__ import annotations

import json
import hashlib
import os
import secrets
import shlex
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("HEYSURE_REPO_ROOT") or Path(__file__).resolve().parents[3]).resolve()
HOST = os.environ.get("HEYSURE_REPO_UPDATER_HOST", "0.0.0.0")
PORT = int(os.environ.get("HEYSURE_REPO_UPDATER_PORT", "58151"))
COMPOSE_CMD = shlex.split(os.environ.get("HEYSURE_REPO_UPDATER_COMPOSE_CMD", "docker compose"))
VERSION_FILE = ROOT / "server" / "data" / "deployed-version.json"


def _read_dotenv_key(name: str) -> str:
    env_path = ROOT / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    prefix = f"{name}="
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or not line.startswith(prefix):
            continue
        value = line[len(prefix):].strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        return value.strip()
    return ""


TOKEN = (
    os.environ.get("HEYSURE_REPO_UPDATER_TOKEN")
    or os.environ.get("HEYSURE_INTERNAL_TOKEN")
    or _read_dotenv_key("HEYSURE_REPO_UPDATER_TOKEN")
    or _read_dotenv_key("HEYSURE_INTERNAL_TOKEN")
    or ""
).strip()

_lock = threading.Lock()
_state: dict[str, Any] = {
    "running": False,
    "phase": "idle",
    "message": "",
    "last_error": "",
    "logs": [],
    "updated_at": time.time(),
}


class UpdateError(RuntimeError):
    pass


def _set_state(**fields: Any) -> None:
    _state.update(fields)
    _state["updated_at"] = time.time()


def _append_log(line: str) -> None:
    text = line.strip()
    if not text:
        return
    logs = list(_state.get("logs") or [])
    logs.append(text)
    _state["logs"] = logs[-120:]
    _state["updated_at"] = time.time()
    print(text, flush=True)


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


def _run_streaming(cmd: list[str], timeout: float = 1800.0) -> None:
    started_at = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            _append_log(line)
            if time.monotonic() - started_at > timeout:
                proc.kill()
                raise UpdateError(f"{' '.join(cmd)} timed out after {int(timeout)}s")
        code = proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
    if code != 0:
        tail = "\n".join((_state.get("logs") or [])[-20:])
        raise UpdateError(f"{' '.join(cmd)} failed with exit code {code}: {tail}")


def _git(args: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return _run(["git", *args], timeout=timeout)


def _git_output(args: list[str], timeout: float = 120.0) -> str:
    return _git(args, timeout=timeout).stdout.strip()


def _discard_local_changes() -> None:
    _git(["reset", "--hard", "HEAD"], timeout=120)
    _git(["clean", "-fd"], timeout=120)


def _reset_to_remote(branch: str, upstream: str) -> None:
    _discard_local_changes()
    if branch:
        _git(["checkout", branch], timeout=120)
    _git(["reset", "--hard", upstream], timeout=120)
    _git(["clean", "-fd"], timeout=120)


def _read_version_file() -> dict[str, Any] | None:
    try:
        payload = json.loads(VERSION_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    current = payload.get("current")
    if not isinstance(current, dict):
        return None
    payload.setdefault("git_available", False)
    payload.setdefault("branch", "")
    return payload


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
    try:
        payload = {"git_available": True, "branch": _branch(), "current": _commit_info("HEAD")}
        if payload["current"]:
            _write_version_file(payload)
            return payload
    except Exception:
        cached = _read_version_file()
        if cached is not None:
            return cached
        raise
    cached = _read_version_file()
    if cached is not None:
        return cached
    return {"git_available": False, "branch": "", "current": None}


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
        _append_log("开始构建 Docker 镜像...")
        _run_streaming([*COMPOSE_CMD, "--progress", "plain", "build"], timeout=1800)
        _set_state(phase="restarting", message="recreating compose services")
        _append_log("开始重建并启动 Docker 容器...")
        _run_streaming([*COMPOSE_CMD, "up", "-d", "--remove-orphans"], timeout=900)
        _append_log("Docker 服务已更新并启动")
        _set_state(running=False, phase="done", message="services updated", last_error="")
    except Exception as exc:
        _set_state(running=False, phase="error", message="update failed", last_error=str(exc))
        _append_log(f"更新失败：{exc}")


def _check_and_update(apply: bool) -> dict[str, Any]:
    if not _lock.acquire(blocking=False):
        return {"ok": False, "busy": True, "state": dict(_state)}
    try:
        _set_state(running=True, phase="checking", message="checking remote updates", last_error="", logs=[])
        _append_log("开始检测远程更新...")
        info = _compare()
        if info["behind"] <= 0:
            _set_state(running=False, phase="up_to_date", message="already up to date")
            return {"ok": True, "updated": False, "update_available": False, **info, "state": dict(_state)}
        if not apply:
            _set_state(running=False, phase="update_available", message=f"{info['behind']} commits available")
            return {"ok": True, "updated": False, "update_available": True, **info, "state": dict(_state)}

        from_sha = ((info.get("current") or {}).get("sha") or "")
        _set_state(phase="pulling", message="pulling latest code")
        _append_log("开始拉取最新代码...")
        _reset_to_remote(str(info.get("branch") or ""), str(info.get("upstream") or ""))
        _git(["submodule", "update", "--init", "--recursive", "--force"], timeout=300)
        version = _version()
        to_sha = ((version.get("current") or {}).get("sha") or "")
        _write_version_file(version)
        _set_state(phase="queued_restart", message="compose rebuild queued")
        _append_log("代码已更新，已排队重建 Docker 服务")
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
        expected = f"Bearer {TOKEN}"
        got = self.headers.get("Authorization", "").strip()
        return secrets.compare_digest(got, expected)

    def _token_fingerprint(self) -> str:
        if not TOKEN:
            return ""
        return hashlib.sha256(TOKEN.encode("utf-8")).hexdigest()[:12]

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {
                "ok": True,
                "root": str(ROOT),
                "state": dict(_state),
                "token_configured": bool(TOKEN),
                "token_fingerprint": self._token_fingerprint(),
            })
            return
        if not self._authorized():
            self._json(401, {
                "ok": False,
                "error": "unauthorized",
                "token_configured": bool(TOKEN),
                "token_fingerprint": self._token_fingerprint(),
            })
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
            self._json(401, {
                "ok": False,
                "error": "unauthorized",
                "token_configured": bool(TOKEN),
                "token_fingerprint": self._token_fingerprint(),
            })
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
    try:
        _version()
    except Exception as exc:
        print(f"repo updater could not collect startup version: {exc}", flush=True)
    token_note = "configured" if TOKEN else "disabled"
    print(f"HeySure repo updater listening on http://{HOST}:{PORT} (root={ROOT}, token={token_note})", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
