"""Process entrypoint for the ``api-gateway`` service (port 3000).

Run with:
    python -m gateway.main
or via uvicorn directly:
    uvicorn gateway.app:sio_app --host 0.0.0.0 --port 3000
"""

import uvicorn

from api.core.logging_config import configure_logging
from api.core.settings import settings
from gateway.app import sio_app  # noqa: F401 — imported for side effects when not reloading


if __name__ == "__main__":
    configure_logging()
    reload_enabled = settings.server_reload
    uvicorn.run(
        "gateway.app:sio_app",
        host="0.0.0.0",
        port=3000,
        reload=reload_enabled,
        log_level="info",
        access_log=True,
        # log_config=None stops uvicorn from installing its own isolated,
        # non-propagating handlers. uvicorn.access (the per-request HTTP log,
        # protocol-level so it also covers Socket.IO polling) then propagates to
        # the root handler from configure_logging() and shows in the launcher
        # console + admin panel ring buffer.
        log_config=None,
        # Watch both gateway/ and api/ so router edits also trigger reload.
        reload_dirs=["main/gateway", "main/api"] if reload_enabled else None,
    )
