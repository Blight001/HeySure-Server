"""``connector-runtime`` process package — Socket.IO + internal HTTP on port 3002.

Hosts the Socket.IO server desktop / browser agents connect to, plus the
``/internal/agent/dispatch`` and ``/internal/feishu/send`` HTTP endpoints
ai-runtime uses to fan work out to connected endpoints. Shared library
code lives in ``api``.
"""

from .app import create_app

__all__ = ["create_app"]
