"""Run coroutines from synchronous worker threads safely.

``ai-runtime`` executes chat runs on dedicated threads and must call async MCP /
HTTP helpers without ``asyncio.run()``, which creates and destroys a fresh event
loop per tool call and can trip "Event loop is closed" when combined with cached
async HTTP clients or pending executor callbacks.

The helpers here auto-heal by recreating the background loop if a previous
misuse (or shutdown) left it closed, so repeated tool calls (including
knowledge.manage) recover instead of permanently breaking the execution path.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Coroutine, Optional, TypeVar

T = TypeVar("T")

_LOOP: Optional[asyncio.AbstractEventLoop] = None
_LOOP_THREAD: Optional[threading.Thread] = None
_LOOP_LOCK = threading.Lock()
_DEFAULT_TIMEOUT = 120.0


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _LOOP, _LOOP_THREAD
    with _LOOP_LOCK:
        if _LOOP is not None and not _LOOP.is_closed() and _LOOP.is_running():
            return _LOOP
        # Previous bridge loop was closed/stopped (e.g. after asyncio.run misuse
        # or process lifecycle); create a fresh one. Old pending callbacks may
        # still emit "Event loop is closed" which callers should tolerate.
        loop = asyncio.new_event_loop()

        def _runner() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=_runner, name="heysure-async-bridge", daemon=True)
        thread.start()
        _LOOP = loop
        _LOOP_THREAD = thread
        return loop


def run_async(coro: Coroutine[Any, Any, T], *, timeout: Optional[float] = None) -> T:
    """Block until ``coro`` completes on a process-wide background event loop."""
    loop = _ensure_loop()
    try:
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout if timeout is not None else _DEFAULT_TIMEOUT)
    except RuntimeError as exc:
        if "Event loop is closed" in str(exc):
            # The bridge loop died (e.g. after a previous short-lived asyncio.run
            # in the same process). Recreate and retry once.
            loop = _ensure_loop()
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            return future.result(timeout=timeout if timeout is not None else _DEFAULT_TIMEOUT)
        raise