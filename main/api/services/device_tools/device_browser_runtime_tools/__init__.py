"""Factory-default browser runtime tools, shipped as program definitions.

Mirrors ``device_runtime_tools`` for desktop: on first browser connect these are
seeded into ``<workspace>/device_tools/browser/`` where they become the editable
source of truth.

Each builtin ``browser_*`` tool gets a thin program wrapper (call builtin + return)
so operators can tweak descriptions / input_schema on the server. The former
server-side generic dispatcher ``browser.run`` is intentionally not generated:
browser capability limits should be visible on the plugin's own tool surface.
"""

import json
import os
from typing import Any, Dict, List

_DIR = os.path.dirname(__file__)

# Builtin browser_* tools removed from the extension catalog; prune workspace copies
# on connect so server dynamic MCP stays aligned with the device.
REMOVED_TOOL_NAMES = frozenset({
    "browser_search",
    "browser_get_content",
    "browser_page_info",
    "browser_find_popups",
    "browser_select",
    "browser_fill_form",
    "browser_hover",
    "browser_dom_snapshot",
    "browser_close_popup",
    "browser.run",
})


def _wrapper_program(builtin_name: str) -> List[Dict[str, Any]]:
    return [
        {"op": "call", "tool": f"builtin:{builtin_name}", "args": "${args}"},
        {"op": "return", "value": "${last}"},
    ]


def _load_catalog() -> List[Dict[str, Any]]:
    with open(os.path.join(_DIR, "catalog.json"), encoding="utf-8") as f:
        return json.load(f)


def load_default_tools() -> List[Dict[str, Any]]:
    catalog = _load_catalog()
    out: List[Dict[str, Any]] = []
    for entry in catalog:
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "description": str(entry.get("description") or ""),
            "input_schema": entry.get("input_schema") if isinstance(entry.get("input_schema"), dict) else {},
            "code_kind": "program",
            "code": _wrapper_program(name),
            "js": "",
            "runtime": "",
            "source": "",
            "permissions": [],
        })
    return out


def sync_workspace_after_catalog_change(user_id: int) -> int:
    """Sync browser builtin wrappers already seeded in a user's workspace.

    Browser tools are exposed to the prompt from the workspace copies, not
    directly from ``catalog.json``. Seeding is intentionally idempotent, so a
    catalog/schema wording change would otherwise leave old prompt text in
    existing ``browser_*.json`` files forever. Keep enabled/disabled state, but
    refresh the builtin wrapper metadata from the current catalog.
    """
    from api.services.device_tools import device_workspace_tools as ws

    changed = 0
    for spec in load_default_tools():
        name = str(spec.get("name") or "").strip()
        if not name:
            continue
        current = ws.get_tool(user_id, "browser", name)
        if not current:
            continue
        if (
            current.get("description") == spec.get("description")
            and current.get("input_schema") == spec.get("input_schema")
            and current.get("code") == spec.get("code")
            and current.get("code_kind") == spec.get("code_kind")
        ):
            continue
        ws.upsert_tool(
            user_id,
            "browser",
            spec,
            enabled=bool(current.get("enabled", True)),
            actor="web",
            action="upsert",
        )
        changed += 1

    for name in REMOVED_TOOL_NAMES:
        if ws.delete_tool(user_id, "browser", name, actor="web"):
            changed += 1
    return changed
