"""Process entrypoint for the ``connector-runtime`` service.

Hosts the Socket.IO server that desktop / browser agents connect to and
the HTTP endpoints used by ai-runtime to dispatch agent tasks and send
outbound Feishu/QQ messages. Both share a single external port (3002).

Required env (see api/core/settings.py for the full list):
    HEYSURE_SERVICE_ROLE=connector       — keeps api.sio's real Socket.IO server
    HEYSURE_INTERNAL_TOKEN=...           — for /internal/* gate
    DATABASE_URL=postgresql://...        — shared with the other services

Run with:
    python -m connector_runtime.main
"""

import os
import sys

import uvicorn


# Must be set BEFORE importing the api package so api.sio binds a real server.
# (Settings reads env at import time; setdefault here ensures the cached
# instance sees ``connector`` even if the operator forgot to export it.)
os.environ.setdefault("HEYSURE_SERVICE_ROLE", "connector")

from api.core.logging_config import configure_logging  # noqa: E402
from api.core.settings import settings  # noqa: E402
from api.runtime.process_control import register_restart_command  # noqa: E402

configure_logging()
register_restart_command([sys.executable, "-m", "connector_runtime.main"])

from connector_runtime.app import create_app  # noqa: E402

app = create_app()


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=settings.connector_runtime_port,
        log_level="info",
        access_log=True,
        # log_config=None lets uvicorn.access propagate to the root handler from
        # configure_logging() so HTTP requests show in the console / admin panel.
        log_config=None,
    )
