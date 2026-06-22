"""Business-logic services.

Each module here owns one domain concern and is imported by routers,
MCP tools, or background workers. Keeping them under ``api.services``
(instead of scattered at the ``api`` package root) makes the boundary
between *infrastructure* (``database``, ``sio``, ``auth``), *services*
(this package), *routers* (HTTP handlers) and *MCP tools* obvious.

Conventions:
- Services depend on ``api.models`` / ``api.database`` and may call
  ``mcp_runtime.mcp`` helpers, but **must not** import from ``api.routers``
  (the dependency direction is router -> service, never reverse).
- Cross-service imports use ``from . import xxx`` (relative within
  the package).
"""
