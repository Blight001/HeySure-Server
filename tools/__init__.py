"""MCP tool handlers grouped by domain.

Each sub-module exposes a set of ``_handler`` callables wired into the
``mcp_runtime.mcp.registry`` module. Tool modules import shared helpers from
``mcp_runtime.mcp.core`` (workspace paths, registry primitives) and
``mcp_runtime.mcp`` intentionally re-exports only the public surface —
internal handlers stay prefixed with ``_`` and are imported by ``registry.py``.
"""
