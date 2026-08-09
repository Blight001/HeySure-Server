"""Context-local structured logging fields shared by all runtimes."""

from __future__ import annotations

import contextlib
import uuid
from contextvars import ContextVar
from typing import Any, Dict, Iterator


_FIELDS = (
    "request_id", "run_id", "task_id", "device_id", "user_id",
    "ai_config_id", "session_id", "tool", "stage", "error_code",
)
_context = {name: ContextVar(f"log_{name}", default=None) for name in _FIELDS}


def values() -> Dict[str, Any]:
    return {name: variable.get() for name, variable in _context.items() if variable.get() is not None}


@contextlib.contextmanager
def bind(**fields: Any) -> Iterator[None]:
    tokens = {
        name: _context[name].set(value)
        for name, value in fields.items()
        if name in _context and value is not None
    }
    try:
        yield
    finally:
        for name, token in tokens.items():
            _context[name].reset(token)


def install_http_request_context(app) -> None:
    @app.middleware("http")
    async def request_context(request, call_next):
        requested = str(request.headers.get("x-request-id") or "").strip()
        request_id = requested[:128] if requested else uuid.uuid4().hex
        with bind(request_id=request_id):
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
