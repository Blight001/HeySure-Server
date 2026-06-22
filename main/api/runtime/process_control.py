"""Best-effort in-place restart for a single service process.

"Restart a service" without an external orchestrator means the process must
relaunch itself. :func:`request_restart` re-execs the process (``os.execv``)
on a background thread shortly after the caller returns, so the HTTP response
that triggered the restart can flush before the old image is replaced. The
new process comes up on the same port with a clean state.

Each entrypoint should call :func:`register_restart_command` at startup to
declare exactly how it was launched (e.g. ``python -m ai_runtime.main``).
Without that we fall back to ``[sys.executable] + sys.argv``, which is correct
for console-script launches (uvicorn) but not for ``python -m pkg.mod`` — the
re-exec would run the module file directly and break package imports — hence
the explicit registration in every ``-m`` entrypoint.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import List, Optional


logger = logging.getLogger(__name__)

_restart_command: Optional[List[str]] = None


def register_restart_command(argv: List[str]) -> None:
    """Declare the command used to relaunch this process on restart."""
    global _restart_command
    _restart_command = list(argv)


def _resolve_command() -> List[str]:
    if _restart_command:
        return list(_restart_command)
    return [sys.executable] + sys.argv


def request_restart(delay: float = 0.5) -> List[str]:
    """Schedule a self-restart on a daemon thread; return the planned command.

    Returning the command (rather than blocking) lets the HTTP handler reply
    ``200`` first; the actual ``execv`` fires ``delay`` seconds later.
    """
    cmd = _resolve_command()

    def _do() -> None:
        time.sleep(delay)
        logger.warning(f"restarting process via execv: {' '.join(cmd)}")
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        try:
            os.execv(cmd[0], cmd)
        except Exception:
            # If re-exec fails, exit non-zero so a supervisor / docker
            # ``restart: unless-stopped`` policy still brings us back.
            logger.exception("execv restart failed; exiting for supervisor restart")
            os._exit(3)

    threading.Thread(target=_do, name="service-restart", daemon=True).start()
    return cmd
