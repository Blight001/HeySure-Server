"""Process entrypoint for the ``ai-runtime`` service.

Consumes ``ChatRun`` rows queued by api-gateway via Postgres NOTIFY and runs
``chat_worker._run_worker`` for each one.

Required env:
    HEYSURE_SERVICE_ROLE=worker         — disables the local Socket.IO server
    HEYSURE_API_GATEWAY_URL=http://api-gateway:3000
                                        — destination for forwarded emits
    HEYSURE_INTERNAL_TOKEN=...          — shared with api-gateway / mcp-runtime
    DATABASE_URL=postgresql://...       — required for LISTEN/NOTIFY
    MCP_RUNTIME_URL=http://mcp-runtime:3001
                                        — optional; enables HTTP tool calls
    CONNECTOR_RUNTIME_URL=http://connector-runtime:3002
                                        — optional; enables HTTP agent dispatch

Run with:
    python -m ai_runtime.main
"""

import logging
import os
import signal
import sys
import threading


# Force-set the service role BEFORE importing the api package so api.sio picks
# the remote proxy at import time. This module must be the entrypoint for the
# worker process.
os.environ.setdefault("HEYSURE_SERVICE_ROLE", "worker")

from api.core.logging_config import configure_logging  # noqa: E402
from api.core.settings import settings  # noqa: E402
from api.runtime.process_control import register_restart_command  # noqa: E402

configure_logging()
register_restart_command([sys.executable, "-m", "ai_runtime.main"])
logger = logging.getLogger(__name__)

from api.database import create_db_and_tables  # noqa: E402
from ai_runtime.worker import run_dispatcher_forever  # noqa: E402


def main() -> int:
    # Warn loudly if the gateway URL is missing — without it _RemoteSio drops
    # every emit silently and the UI never updates while AI runs progress.
    if not settings.api_gateway_url:
        logger.warning(
            "HEYSURE_API_GATEWAY_URL not set. Socket.IO events from this worker "
            "will be dropped — UI status updates will be missing. Set "
            "HEYSURE_API_GATEWAY_URL=http://api-gateway:3000 in your environment."
        )
    if not settings.internal_token:
        logger.warning(
            "HEYSURE_INTERNAL_TOKEN not set. Cross-service calls will fall back "
            "to loopback-only auth, which will fail in containerized deployments."
        )

    create_db_and_tables()

    # Expose a minimal health/console-tail HTTP surface so the admin panel can
    # monitor this otherwise-headless worker. Best-effort: never fatal.
    try:
        from ai_runtime.internal_app import start_status_server_in_thread

        start_status_server_in_thread(settings.ai_runtime_port)
    except Exception:
        logger.exception("ai-runtime status server bootstrap failed")

    stop_evt = threading.Event()

    def _on_signal(signum, _frame):
        logger.info(f"signal {signum} -> draining and exiting")
        stop_evt.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    run_dispatcher_forever(stop_evt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
