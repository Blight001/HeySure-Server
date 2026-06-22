"""Tiny ``/internal/*`` HTTP surface for the ai-runtime worker.

The worker process is normally headless — it consumes ``ChatRun`` rows from
the queue and has no HTTP server. The admin panel, however, wants to show the
same "is it up / what is it doing / recent console output" view for every
sub-service, including this one.

This module exposes a minimal FastAPI app (health + console tail) gated by the
shared ``INTERNAL_TOKEN``. :func:`start_status_server_in_thread` runs it on a
daemon thread so it never blocks the dispatcher loop.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, FastAPI

from api.runtime.internal_http import require_internal_token


logger = logging.getLogger(__name__)


def create_status_app() -> FastAPI:
    app = FastAPI(title="HeySure AI Runtime Status")
    router = APIRouter(prefix="/internal", dependencies=[Depends(require_internal_token)])

    @router.get("/health")
    def health() -> Dict[str, Any]:
        from ai_runtime.worker import active_runs

        runs = active_runs()
        return {"ok": True, "role": "worker", **runs}

    @router.get("/logs")
    def logs(limit: int = 200, level: Optional[str] = None) -> Dict[str, Any]:
        from api.core.logging_config import get_recent_logs

        return {"ok": True, "lines": get_recent_logs(limit=limit, level=level)}

    @router.post("/restart")
    def restart() -> Dict[str, Any]:
        from api.runtime.process_control import request_restart

        cmd = request_restart()
        logger.warning("restart requested via /internal/restart")
        return {"ok": True, "restarting": True, "command": cmd}

    app.include_router(router)
    return app


def start_status_server_in_thread(port: int) -> Optional[threading.Thread]:
    """Launch the status app on a daemon thread; return the thread (or None).

    Failures here must never take the worker down — monitoring is best-effort,
    so an import/bind error is logged and swallowed.
    """
    try:
        import uvicorn

        config = uvicorn.Config(
            create_status_app(),
            host="0.0.0.0",
            port=port,
            log_level="info",
            access_log=True,
            # log_config=None lets uvicorn.access propagate to the root handler
            # from configure_logging() so this worker's HTTP requests show in the
            # console / admin panel like the other three backends.
            log_config=None,
            # The worker owns SIGINT/SIGTERM; uvicorn must not install its own.
            lifespan="off",
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None  # type: ignore[assignment]

        thread = threading.Thread(
            target=server.run, name="ai-runtime-status", daemon=True
        )
        thread.start()
        logger.info(f"ai-runtime status server listening on :{port}")
        return thread
    except Exception:
        logger.exception("failed to start ai-runtime status server")
        return None
