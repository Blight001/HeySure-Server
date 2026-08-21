"""Authenticated HTTP boundary for the host repository updater."""

from __future__ import annotations

import json
import secrets
import time
from http.server import BaseHTTPRequestHandler
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit


def make_handler(context: Mapping[str, Any]) -> type[BaseHTTPRequestHandler]:
    token = str(context["token"])
    default_limit = int(context["default_version_limit"])

    class Handler(BaseHTTPRequestHandler):
        server_version = "HeySureRepoUpdater/1.0"

        def _authorized(self) -> bool:
            if len(token) < 32:
                return False
            expected = f"Bearer {token}"
            return secrets.compare_digest(self.headers.get("Authorization", "").strip(), expected)

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _reject_unauthorized(self) -> bool:
            if self._authorized():
                return False
            self._json(
                401,
                {
                    "ok": False,
                    "error": "unauthorized",
                    "token_configured": len(token) >= 32,
                },
            )
            return True

        def do_GET(self) -> None:
            request = urlsplit(self.path)
            if request.path == "/health":
                self._json(200, {"ok": True, "token_configured": len(token) >= 32})
                return
            if self._reject_unauthorized():
                return
            try:
                if request.path == "/version":
                    self._json(200, context["version"]())
                elif request.path == "/versions":
                    raw_limit = parse_qs(request.query).get("limit", [str(default_limit)])[0]
                    self._json(200, context["version_history"](int(raw_limit)))
                elif request.path == "/state":
                    self._json(200, {"ok": True, "state": dict(context["state"])})
                else:
                    self._json(404, {"ok": False, "error": "not found"})
            except (TypeError, ValueError):
                self._json(400, {"ok": False, "error": "invalid request parameters"})
            except Exception:
                self._json(500, {"ok": False, "error": "repository metadata request failed"})

        def _read_payload(self) -> dict[str, Any] | None:
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self._json(400, {"ok": False, "error": "invalid Content-Length"})
                return None
            if length < 0 or length > 16_384:
                self.close_connection = True
                self._json(413, {"ok": False, "error": "request body is too large"})
                return None
            try:
                payload = json.loads((self.rfile.read(length) if length else b"{}").decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                self._json(400, {"ok": False, "error": "request body must be valid JSON"})
                return None
            if not isinstance(payload, dict):
                self._json(400, {"ok": False, "error": "request body must be a JSON object"})
                return None
            return payload

        def do_POST(self) -> None:
            if self._reject_unauthorized():
                return
            payload = self._read_payload()
            if payload is None:
                return
            path = urlsplit(self.path).path
            if path == "/check":
                result = context["check"](bool(payload.get("apply", True)))
                status = 409 if result.get("busy") or result.get("blocked") else 200
                self._json(status, result)
            elif path == "/rollback":
                status, result = context["rollback"](
                    str(payload.get("target_sha") or ""),
                    str(payload.get("operation_id") or ""),
                )
                self._json(status, result)
            elif path == "/rebuild":
                result = context["rebuild"]()
                self._json(409 if result.get("busy") else 200, result)
            else:
                self._json(404, {"ok": False, "error": "not found"})

        def log_message(self, fmt: str, *args: Any) -> None:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{timestamp}] {self.address_string()} {fmt % args}", flush=True)

    return Handler
