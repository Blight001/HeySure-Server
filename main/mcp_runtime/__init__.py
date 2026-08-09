"""``mcp-runtime`` process package — internal HTTP wrapper on port 3001.

Exposes the in-process MCP tool registry over ``/internal/mcp/*`` so
api-gateway and ai-runtime can call tools without holding a direct
Python reference. Shared library code lives in ``api``.
"""

# Keep package import side-effect free. Entrypoints import ``mcp_runtime.app``
# explicitly; importing a helper such as ``mcp_runtime.mcp.core`` must not build
# a FastAPI application or initialize the tool registry.
