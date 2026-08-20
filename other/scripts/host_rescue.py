#!/usr/bin/env python3
"""Independent host recovery plane for the HeySure Compose deployment.

This service runs under systemd, outside the application Compose project.  It
therefore stays reachable when API Gateway cannot boot.  Only a small allowlist
of recovery actions is exposed; arbitrary commands and database operations are
intentionally unavailable.
"""

from __future__ import annotations

import json
import os
import secrets
import shlex
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


HOST = os.environ.get("HEYSURE_RESCUE_HOST", "0.0.0.0")
PORT = int(os.environ.get("HEYSURE_RESCUE_PORT", "58152"))
COMPOSE_DIR = Path(
    os.environ.get("HEYSURE_COMPOSE_DIR")
    or os.environ.get("HEYSURE_RESCUE_ROOT")
    or Path(__file__).resolve().parents[4]
).resolve()
COMPOSE_CMD = shlex.split(os.environ.get("HEYSURE_RESCUE_COMPOSE_CMD", "docker compose"))
AUTO_RECOVER = os.environ.get("HEYSURE_RESCUE_AUTO_RECOVER", "true").lower() in {"1", "true", "yes", "on"}
CHECK_INTERVAL = max(10, int(os.environ.get("HEYSURE_RESCUE_CHECK_INTERVAL", "15")))
FAILURE_THRESHOLD = max(3, int(os.environ.get("HEYSURE_RESCUE_FAILURE_THRESHOLD", "6")))
RECOVERY_COOLDOWN = max(60, int(os.environ.get("HEYSURE_RESCUE_COOLDOWN", "300")))
ALLOWED_ORIGINS = {
    value.strip().rstrip("/")
    for value in os.environ.get("HEYSURE_RESCUE_ALLOWED_ORIGINS", "").split(",")
    if value.strip()
}


def _read_dotenv_key(name: str) -> str:
    try:
        lines = (COMPOSE_DIR / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    prefix = f"{name}="
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or not line.startswith(prefix):
            continue
        value = line[len(prefix):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        return value.strip()
    return ""


TOKEN = (os.environ.get("HEYSURE_RESCUE_TOKEN") or _read_dotenv_key("HEYSURE_RESCUE_TOKEN")).strip()
SERVICES = ("api-gateway", "mcp-runtime", "connector-runtime", "ai-runtime")
ACTION_SERVICES = {
    "restart_gateway": ("api-gateway",),
    "restart_runtimes": SERVICES,
}
_action_lock = threading.Lock()
_state_lock = threading.Lock()
_state: dict[str, Any] = {
    "running": False,
    "action": "",
    "message": "ready",
    "last_error": "",
    "updated_at": time.time(),
    "last_auto_recovery_at": 0.0,
}


class RescueError(RuntimeError):
    """A safe-to-classify host recovery failure."""


def _set_state(**fields: Any) -> None:
    with _state_lock:
        _state.update(fields)
        _state["updated_at"] = time.time()


def state_snapshot() -> dict[str, Any]:
    with _state_lock:
        return dict(_state)


def _run_compose(*args: str, timeout: float = 90.0) -> str:
    try:
        result = subprocess.run(
            [*COMPOSE_CMD, *args],
            cwd=str(COMPOSE_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RescueError("compose command unavailable or timed out") from exc
    if result.returncode:
        raise RescueError("compose recovery command failed")
    return result.stdout


def _parse_compose_rows(raw: str) -> list[dict[str, Any]]:
    text = raw.strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, list) else [payload]
    except ValueError:
        rows = []
        for line in text.splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                rows.append(item)
        return rows


def service_statuses() -> list[dict[str, str]]:
    rows = _parse_compose_rows(_run_compose("ps", "--all", "--format", "json", timeout=30))
    by_service = {str(row.get("Service") or ""): row for row in rows}
    statuses = []
    for service in SERVICES:
        row = by_service.get(service, {})
        statuses.append({
            "service": service,
            "state": str(row.get("State") or "missing"),
            "health": str(row.get("Health") or ""),
            "status": str(row.get("Status") or "not created"),
        })
    return statuses


def _recover_worker(action: str, automatic: bool) -> None:
    services = ACTION_SERVICES[action]
    try:
        _set_state(running=True, action=action, message="recovery in progress", last_error="")
        for service in services:
            _run_compose("up", "-d", "--no-deps", "--force-recreate", service, timeout=180)
        fields: dict[str, Any] = {
            "running": False,
            "action": action,
            "message": "recovery command completed",
            "last_error": "",
        }
        if automatic:
            fields["last_auto_recovery_at"] = time.time()
        _set_state(**fields)
    except RescueError as exc:
        _set_state(running=False, action=action, message="recovery failed", last_error=str(exc))
    finally:
        _action_lock.release()


def queue_recovery(action: str, *, automatic: bool = False) -> dict[str, Any]:
    if action not in ACTION_SERVICES:
        return {"ok": False, "error": "unsupported recovery action"}
    if not _action_lock.acquire(blocking=False):
        return {"ok": False, "busy": True, "error": "another recovery is running"}
    thread = threading.Thread(
        target=_recover_worker,
        args=(action, automatic),
        name=f"heysure-rescue-{action}",
        daemon=True,
    )
    thread.start()
    return {"ok": True, "started": True, "action": action}


def _gateway_is_running() -> bool:
    try:
        output = _run_compose("ps", "--status", "running", "-q", "api-gateway", timeout=20)
    except RescueError:
        return True
    return bool(output.strip())


def auto_recovery_loop() -> None:
    failures = 0
    while True:
        time.sleep(CHECK_INTERVAL)
        failures = 0 if _gateway_is_running() else failures + 1
        last = float(state_snapshot().get("last_auto_recovery_at") or 0)
        if failures < FAILURE_THRESHOLD or time.time() - last < RECOVERY_COOLDOWN:
            continue
        result = queue_recovery("restart_gateway", automatic=True)
        if result.get("started"):
            failures = 0


def _origin_allowed(origin: str, host_header: str) -> bool:
    if not origin:
        return True
    normalized = origin.rstrip("/")
    if normalized in ALLOWED_ORIGINS:
        return True
    try:
        origin_host = (urlsplit(normalized).hostname or "").lower()
        request_host = host_header.rsplit(":", 1)[0].strip("[]").lower()
    except ValueError:
        return False
    return bool(origin_host and secrets.compare_digest(origin_host, request_host))


class Handler(BaseHTTPRequestHandler):
    server_version = "HeySureHostRescue/1.0"

    def _authorized(self) -> bool:
        expected = f"Bearer {TOKEN}"
        return bool(TOKEN) and secrets.compare_digest(self.headers.get("Authorization", "").strip(), expected)

    def _cors_origin(self) -> str:
        origin = self.headers.get("Origin", "").strip()
        return origin if _origin_allowed(origin, self.headers.get("Host", "")) else ""

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        if not self._cors_origin():
            self._json(403, {"ok": False, "error": "origin not allowed"})
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self._cors_origin())
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"ok": True, "service": "host-rescue", "auto_recover": AUTO_RECOVER})
            return
        if not self._authorized():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            if self.path == "/api/status":
                self._json(200, {"ok": True, "services": service_statuses(), "recovery": state_snapshot()})
            else:
                self._json(404, {"ok": False, "error": "not found"})
        except RescueError:
            self._json(503, {"ok": False, "error": "compose status unavailable"})

    def do_POST(self) -> None:
        if not self._authorized():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        length = min(int(self.headers.get("Content-Length") or "0"), 4096)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except ValueError:
            payload = {}
        if self.path != "/api/recover":
            self._json(404, {"ok": False, "error": "not found"})
            return
        result = queue_recovery(str(payload.get("action") or ""))
        self._json(409 if result.get("busy") else (200 if result.get("ok") else 400), result)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {self.address_string()} {fmt % args}", flush=True)


def main() -> None:
    if len(TOKEN) < 32:
        raise SystemExit("HEYSURE_RESCUE_TOKEN must contain at least 32 characters")
    if not (COMPOSE_DIR / "docker-compose.yml").is_file():
        raise SystemExit("HEYSURE_COMPOSE_DIR must point to the managed Compose workspace")
    if AUTO_RECOVER:
        threading.Thread(target=auto_recovery_loop, name="heysure-rescue-watchdog", daemon=True).start()
    print(f"HeySure host rescue listening on {HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
