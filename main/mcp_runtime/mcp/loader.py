"""Hot-reload entry for the MCP tool registry.

Goal: let a deployer drop new code into ``server/tools/`` (toolbox device) and pick it
up at runtime without restarting the process. Reload is admin-level — never
expose this to end users.

Flow:
1. ``importlib.reload`` every module under ``tools.*`` so handler
   bodies pick up source changes.
2. Build a fresh ``MCPRegistry`` and call ``_register_builtin_tools`` on
   it (the function from ``mcp_runtime.mcp.registry``).
3. On success, swap ``registry._tools`` in place via ``replace_tools``
   so existing references (held by ``chat_worker``, routers, etc.)
   see the new tool set without needing to be re-bound.

On failure at any step before the swap, the live registry is untouched.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from typing import Any, Dict, List

from .core import MCPRegistry


_TOOLS_PACKAGE = "tools"
_REGISTRY_MODULE = "mcp_runtime.mcp.registry"


def _iter_tool_modules() -> List[str]:
    """Return fully-qualified names of every module under server/tools/ (toolbox handlers)."""
    try:
        tools_pkg = importlib.import_module(_TOOLS_PACKAGE)
    except Exception:
        return []
    names: List[str] = []
    for info in pkgutil.iter_modules(tools_pkg.__path__):
        if info.ispkg or info.name in {"engine", "__init__"}:
            continue
        names.append(f"{_TOOLS_PACKAGE}.{info.name}")
    return names


def _reload_module(mod_name: str) -> None:
    if mod_name in sys.modules:
        importlib.reload(sys.modules[mod_name])
    else:
        importlib.import_module(mod_name)


def reload_registry() -> Dict[str, Any]:
    """Rebuild the live MCP registry from source. Returns a status payload.

    Returns:
        {
            "ok": bool,
            "version": int,                 # new version after reload
            "tools": int,                   # number of tools after reload
            "plugin_errors": [],            # always empty (plugins extension removed)
            "error": Optional[str],         # set when ok=False
        }
    """
    # Import the module explicitly. ``mcp_runtime.mcp`` re-exports a ``registry``
    # singleton object, so ``from . import registry`` would bind the object
    # instead of the module and break attribute access like ``registry.version``.
    registry_module = importlib.import_module(_REGISTRY_MODULE)

    try:
        for mod_name in _iter_tool_modules():
            try:
                _reload_module(mod_name)
            except Exception as exc:
                # If a tool module fails to reload, treat it as fatal — the
                # builtin tool set must remain coherent.
                return {
                    "ok": False,
                    "version": registry_module.registry.version,
                    "tools": len(registry_module.registry._tools),
                    "plugin_errors": [],
                    "error": f"tool module {mod_name}: {exc}",
                }

        # Bring the registration function back into scope after tool reloads.
        _reload_module(_REGISTRY_MODULE)
        register_fn = getattr(sys.modules[_REGISTRY_MODULE], "_register_builtin_tools")

        fresh = MCPRegistry()
        register_fn(fresh)

        registry_module.registry.replace_tools(fresh._tools)

        return {
            "ok": True,
            "version": registry_module.registry.version,
            "tools": len(registry_module.registry._tools),
            "plugin_errors": [],
            "error": None,
        }
    except Exception as exc:
        return {
            "ok": False,
            "version": registry_module.registry.version,
            "tools": len(registry_module.registry._tools),
            "plugin_errors": [],
            "error": f"reload failed: {exc}",
        }


def load_plugins_on_startup() -> Dict[str, Any]:
    """No-op kept for backward compatibility.

    Builtin tools are registered at ``mcp_runtime.mcp.registry`` import time.
    The plugins extension point has been removed.
    """
    registry_module = importlib.import_module(_REGISTRY_MODULE)
    return {
        "loaded": 0,
        "version": registry_module.registry.version,
        "tools": len(registry_module.registry._tools),
        "plugin_errors": [],
    }
