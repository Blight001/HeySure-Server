"""Process-wide ``logging`` setup.

Call :func:`configure_logging` once per process at startup. Module code
then does the usual ``logger = logging.getLogger(__name__)`` and uses
``logger.info / warning / error / debug`` instead of ``print``.

Two formats:
- ``settings.log_json == False`` (default) — human-readable, optionally
  ANSI-colored when stdout is a TTY. Good for ``docker logs`` followed
  by a real human.
- ``settings.log_json == True`` — one JSON object per line. Drops
  cleanly into Loki / CloudWatch / Datadog without parsing tricks.

``settings.log_level`` picks the root threshold (DEBUG / INFO / WARNING /
ERROR). HTTP access logs stay visible at INFO so the launcher console shows
ordinary requests, while chatty client libraries are dialled down.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import re
import sys
import threading
from typing import Any, Dict, List, Optional

from .settings import settings


_CONFIGURED = False

# Max console lines kept in memory per process for the admin panel.
_RING_BUFFER_CAPACITY = 600


# Patterns whose secret portion must never reach the admin panel's log view.
# Order matters: more specific (key=value) patterns run before the broad
# "looks like an API key" sweep.
_REDACT_PATTERNS = (
    # Authorization: Bearer <token>
    (re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._\-]{6,})"), r"\1***"),
    # key/secret/token/password = "value"  (json or kv form)
    (
        re.compile(
            r"(?i)(\"?(?:api[_-]?key|secret|token|password|hashed_password|app_secret|"
            r"verification_token|jwt_secret|internal_token)\"?\s*[:=]\s*\"?)"
            r"([^\"\s,}&]+)"
        ),
        r"\1***",
    ),
    # OpenAI / DeepSeek style standalone keys
    (re.compile(r"\bsk-[A-Za-z0-9]{6,}\b"), "sk-***"),
    # Tavily keys
    (re.compile(r"\btvly-[A-Za-z0-9]{6,}\b"), "tvly-***"),
)


def redact_secrets(text: str) -> str:
    """Mask obvious credentials in a log line for the admin console.

    Only applied to the in-memory buffer the admin panel reads — the raw
    stdout stream the operator owns is left untouched.
    """
    if not text:
        return text
    for pattern, repl in _REDACT_PATTERNS:
        try:
            text = pattern.sub(repl, text)
        except Exception:
            continue
    return text


class RingBufferHandler(logging.Handler):
    """Keep the most recent log records in memory so the admin panel can show
    a service's console output without shelling into the container.

    Records are stored as plain dicts (ts/level/logger/msg) in a bounded,
    thread-safe deque. This is intentionally lightweight — it is a tail, not
    a durable log store.
    """

    def __init__(self, capacity: int = _RING_BUFFER_CAPACITY) -> None:
        super().__init__()
        self._buffer: "collections.deque[Dict[str, Any]]" = collections.deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message = f"{message}\n{logging.Formatter().formatException(record.exc_info)}"
            message = redact_secrets(message)
        except Exception:
            return
        with self._lock:
            self._seq += 1
            self._buffer.append(
                {
                    "seq": self._seq,
                    "ts": record.created,
                    "level": record.levelname,
                    "logger": redact_secrets(record.name),
                    "msg": message,
                }
            )

    def snapshot(self, limit: int = 200, level: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._buffer)
        if level:
            wanted = level.upper()
            items = [it for it in items if it["level"] == wanted]
        if limit and limit > 0:
            items = items[-limit:]
        return items


_ring_buffer_handler: Optional[RingBufferHandler] = None


def get_recent_logs(limit: int = 200, level: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return the most recent in-process log lines captured by the ring buffer.

    Returns an empty list if logging hasn't been configured yet (e.g. during
    very early import) so callers never have to guard against ``None``.
    """
    if _ring_buffer_handler is None:
        return []
    return _ring_buffer_handler.snapshot(limit=limit, level=level)


# ---- Formatters -------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    """One JSON object per record. Stable shape for log aggregators."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Anything attached via logger.X("...", extra={"key": value})
        for key, value in record.__dict__.items():
            if key in (
                "args", "asctime", "created", "exc_info", "exc_text", "filename",
                "funcName", "levelname", "levelno", "lineno", "module",
                "msecs", "message", "msg", "name", "pathname", "process",
                "processName", "relativeCreated", "stack_info", "thread", "threadName",
                "taskName",
            ):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except Exception:
                payload[key] = repr(value)
        return json.dumps(payload, ensure_ascii=False)


_LEVEL_COLORS = {
    "DEBUG": "\033[37m",     # gray
    "INFO": "\033[36m",      # cyan
    "WARNING": "\033[33m",   # yellow
    "ERROR": "\033[31m",     # red
    "CRITICAL": "\033[1;31m",  # bold red
}
_RESET = "\033[0m"


class _ConsoleFormatter(logging.Formatter):
    """Human-readable formatter with optional ANSI level color."""

    def __init__(self, use_color: bool) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
            datefmt="%H:%M:%S",
        )
        self._use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if not self._use_color:
            return text
        color = _LEVEL_COLORS.get(record.levelname, "")
        return f"{color}{text}{_RESET}" if color else text


def _stdout_is_tty() -> bool:
    try:
        return bool(sys.stdout.isatty()) and not os.environ.get("NO_COLOR")
    except Exception:
        return False


# ---- Public API -------------------------------------------------------------


def is_configured() -> bool:
    """Whether :func:`configure_logging` has already installed app logging.

    Alembic's ``env.py`` checks this so that migrations running in-process at
    startup do NOT call ``logging.config.fileConfig`` — that would disable every
    existing logger (uvicorn.access, uvicorn.error, gateway.app, …) and replace
    the root handlers, silencing all logs (incl. the HTTP access log) for the
    rest of the process's life. Standalone ``alembic`` CLI runs still get the
    ini's logging because this returns ``False`` there.
    """
    return _CONFIGURED


def configure_logging() -> None:
    """Idempotently install the root logging configuration.

    Safe to call multiple times — second and later calls are no-ops so each
    process entrypoint can call this without coordinating who "owns" setup.
    """
    global _CONFIGURED, _ring_buffer_handler
    if _CONFIGURED:
        return
    _CONFIGURED = True

    handler = logging.StreamHandler(stream=sys.stdout)
    if settings.log_json:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(_ConsoleFormatter(use_color=_stdout_is_tty()))

    # In-memory tail consumed by the admin panel's service console view.
    _ring_buffer_handler = RingBufferHandler()

    root = logging.getLogger()
    root.setLevel(settings.log_level)
    # Replace any prior handlers so re-runs in tests don't double-print.
    root.handlers[:] = [handler, _ring_buffer_handler]

    # Keep HTTP request lines visible in the launcher console.
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)

    # Dial chatty third-party client/watch loggers down one notch so the
    # operator's INFO logs don't drown in framework chatter.
    for noisy in ("httpx", "httpcore", "watchfiles"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
