"""Declarative builtin MCP registry assembly."""

from .builtin_catalog import BUILTIN_TOOLS
from .core import MCPRegistry


def _register_builtin_tools(registry: MCPRegistry) -> None:
    for tool in BUILTIN_TOOLS:
        registry.register(tool)


registry = MCPRegistry()
_register_builtin_tools(registry)
