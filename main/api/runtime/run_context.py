"""Process-local MCP run context propagated across async/thread boundaries."""

from __future__ import annotations

import contextvars
from typing import Any, Dict, Optional


_RUN_SESSION_CONTEXT: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "run_session_context", default=None
)


def set_run_session_context(ctx: Optional[Dict[str, Any]]):
    return _RUN_SESSION_CONTEXT.set(ctx or None)


def reset_run_session_context(token) -> None:
    _RUN_SESSION_CONTEXT.reset(token)


def get_run_session_context() -> Optional[Dict[str, Any]]:
    return _RUN_SESSION_CONTEXT.get()
