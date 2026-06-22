"""``api-gateway`` process package — public HTTP / Socket.IO façade on port 3000.

Holds the FastAPI app that mounts ``gateway.routers.*`` and the Socket.IO
wrapper exposed to browsers / desktop agents. Worker / MCP / connector
runtimes live in their own top-level packages and import shared library
code from ``api`` instead.
"""
