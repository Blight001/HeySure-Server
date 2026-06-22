"""``mcp-runtime`` process package — internal HTTP wrapper on port 3001.

Exposes the in-process MCP tool registry over ``/internal/mcp/*`` so
api-gateway and ai-runtime can call tools without holding a direct
Python reference. Shared library code lives in ``api``.
"""

from .app import create_app

__all__ = ["create_app"]
