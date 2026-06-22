"""Process entrypoint for the ``mcp-runtime`` service (port 3001).

Run with:
    HEYSURE_INTERNAL_TOKEN=... DATABASE_URL=... \
    python -m mcp_runtime.main

or via uvicorn directly:
    uvicorn mcp_runtime.app:create_app --factory --port 3001
"""

import os
import sys

import uvicorn


# Force the MCP worker role before importing the api package so any shared
# Socket.IO helpers stay in proxy mode instead of binding a local server.
os.environ.setdefault("HEYSURE_SERVICE_ROLE", "mcp")

from api.core.logging_config import configure_logging  # noqa: E402
from api.core.settings import settings  # noqa: E402
from api.runtime.process_control import register_restart_command  # noqa: E402

configure_logging()
register_restart_command([sys.executable, "-m", "mcp_runtime.main"])

from mcp_runtime.app import create_app  # noqa: E402

app = create_app()


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=settings.mcp_runtime_port,
        log_level="info",
        access_log=True,
        # log_config=None lets uvicorn.access propagate to the root handler from
        # configure_logging() so HTTP requests show in the console / admin panel.
        log_config=None,
    )
