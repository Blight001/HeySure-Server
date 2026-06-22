"""Run one fixed deployment script through a token-protected HTTP webhook."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", required=True, help="Absolute path to the fixed update script")
    parser.add_argument("--repo", help="Git repository directory; defaults to the script directory")
    parser.add_argument("--token", required=True, help="Bearer token required from callers")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    script = Path(args.script).expanduser().resolve(strict=True)
    if not script.is_file():
        parser.error("--script must point to a file")
    repo = Path(args.repo).expanduser().resolve(strict=True) if args.repo else script.parent
    if not (repo / ".git").exists():
        parser.error("--repo must point to a Git working tree")

    run_lock = threading.Lock()
    state_lock = threading.Lock()
    state = {
        "running": False,
        "status": "idle",
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "logs": deque(maxlen=300),
    }
    version_lock = threading.Lock()
    version_state = {"branch": "", "ahead": 0, "behind": 0, "current": None, "remote": None}

    def git(*git_args: str, timeout: float = 60.0) -> str:
        result = subprocess.run(
            ["git", *git_args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return result.stdout.strip()

    def commit_info(ref: str) -> dict:
        values = git("log", "-1", "--format=%H%n%h%n%an%n%ct%n%s", ref).split("\n", 4)
        body = git("show", "-s", "--format=%B", ref)
        files = []
        for line in git("show", "--format=", "--numstat", ref).splitlines()[:200]:
            added, deleted, path = (line.split("\t", 2) + ["", "", ""])[:3]
            if path:
                files.append({"path": path, "added": None if added == "-" else int(added), "deleted": None if deleted == "-" else int(deleted)})
        return {
            "sha": values[0],
            "short": values[1],
            "author": values[2],
            "committed_at": float(values[3]),
            "subject": values[4],
            "body": body,
            "files": files,
        }

    def read_version(*, fetch: bool) -> dict:
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        if fetch:
            git("fetch", "--quiet", "origin", branch, timeout=180.0)
        upstream = f"origin/{branch}"
        current = commit_info("HEAD")
        remote = commit_info(upstream)
        counts = git("rev-list", "--left-right", "--count", f"HEAD...{upstream}").split()
        payload = {
            "branch": branch,
            "ahead": int(counts[0]),
            "behind": int(counts[1]),
            "current": current,
            "remote": remote,
            "checked_at": time.time(),
        }
        with version_lock:
            version_state.update(payload)
        return payload

    try:
        read_version(fetch=False)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        pass

    def run_script() -> None:
        exit_code = -1
        try:
            with state_lock:
                state.update(
                    running=True,
                    status="running",
                    started_at=time.time(),
                    finished_at=None,
                    exit_code=None,
                )
                state["logs"].clear()
            process = subprocess.Popen(
                [str(script)],
                cwd=str(script.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            assert process.stdout is not None
            for raw_line in process.stdout:
                line = raw_line.rstrip("\r\n")
                with state_lock:
                    state["logs"].append(line)
                print(f"deploy-update: {line}", flush=True)
            exit_code = process.wait()
            with state_lock:
                state.update(
                    running=False,
                    status="success" if exit_code == 0 else "error",
                    finished_at=time.time(),
                    exit_code=exit_code,
                )
        except Exception as exc:
            with state_lock:
                state["logs"].append(f"Webhook runner error: {exc}")
                state.update(
                    running=False,
                    status="error",
                    finished_at=time.time(),
                    exit_code=-1,
                )
        finally:
            run_lock.release()
            if exit_code == 0:
                # The deployment may have replaced this file. Let systemd's
                # Restart=always launch a fresh process with the new code.
                threading.Timer(2.0, lambda: os._exit(0)).start()

    class Handler(BaseHTTPRequestHandler):
        def _authorized(self) -> bool:
            expected = f"Bearer {args.token}"
            return hmac.compare_digest(self.headers.get("Authorization", ""), expected)

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            if self.path not in ("/status", "/version"):
                self._json(404, {"ok": False, "error": "not found"})
                return
            if not self._authorized():
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            if self.path == "/version":
                try:
                    payload = read_version(fetch=False)
                except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
                    self._json(500, {"ok": False, "error": str(exc)})
                    return
                self._json(200, {"ok": True, **payload})
                return
            with state_lock:
                payload = {key: value for key, value in state.items() if key != "logs"}
                payload["logs"] = list(state["logs"])
            self._json(200, {"ok": True, **payload})

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            if self.path not in ("/check", "/update"):
                self._json(404, {"ok": False, "error": "not found"})
                return
            if not self._authorized():
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            if self.path == "/check":
                if run_lock.locked():
                    self._json(409, {"ok": False, "error": "update already running"})
                    return
                try:
                    payload = read_version(fetch=True)
                except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
                    self._json(500, {"ok": False, "error": str(exc)})
                    return
                self._json(200, {"ok": True, **payload})
                return
            if not run_lock.acquire(blocking=False):
                self._json(409, {"ok": False, "error": "update already running"})
                return
            threading.Thread(target=run_script, name="deploy-update", daemon=True).start()
            self._json(202, {"ok": True, "accepted_at": time.time()})

        def log_message(self, fmt: str, *values: object) -> None:
            print(f"deploy-webhook: {self.address_string()} {fmt % values}", flush=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"deploy-webhook listening on http://{args.host}:{args.port}/update", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
