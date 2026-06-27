"""Factory-default browser runtime tools, shipped as program definitions.

Mirrors ``device_runtime_tools`` for desktop: on first browser connect these are
seeded into ``<workspace>/device_tools/browser/`` where they become the editable
source of truth.

Each builtin ``browser_*`` tool gets a thin program wrapper (call builtin + return)
so operators can tweak descriptions / input_schema on the server. A meta
``browser.run`` tool dispatches to any builtin by name (like ``shell.run``).
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
})


def _wrapper_program(builtin_name: str) -> List[Dict[str, Any]]:
    return [
        {"op": "call", "tool": f"builtin:{builtin_name}", "args": "${args}"},
        {"op": "return", "value": "${last}"},
    ]


def _browser_run_tool(catalog: List[Dict[str, Any]]) -> Dict[str, Any]:
    names = sorted(t["name"] for t in catalog)
    return {
        "name": "browser.run",
        "description": (
            "统一浏览器动作调度入口（类似 shell.run）：指定 tool 与 params，转发到任意内置 "
            "browser_* 工具。可在服务器工作区直接修改本工具的 schema、说明与默认参数描述。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tool": {
                    "type": "string",
                    "enum": names,
                    "description": "要调用的内置浏览器工具名（browser_*）。",
                },
                "params": {
                    "type": "object",
                    "description": "传给目标工具的参数对象；字段随 tool 变化，见各 browser_* 工具 schema。",
                },
            },
            "required": ["tool"],
        },
        "code_kind": "program",
        "code": [
            {"op": "call", "tool": "${args.tool}", "args": "${args.params}"},
            {"op": "return", "value": "${last}"},
        ],
        "js": "",
        "runtime": "",
        "source": "",
        "permissions": [],
    }


def _load_catalog() -> List[Dict[str, Any]]:
    with open(os.path.join(_DIR, "catalog.json"), encoding="utf-8") as f:
        return json.load(f)


def load_default_tools() -> List[Dict[str, Any]]:
    catalog = _load_catalog()
    out: List[Dict[str, Any]] = [_browser_run_tool(catalog)]
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
    """Drop removed builtin wrappers and refresh ``browser.run`` enum from catalog."""
    from api.services.device_tools import device_workspace_tools as ws

    removed = 0
    for name in REMOVED_TOOL_NAMES:
        if ws.delete_tool(user_id, "browser", name, actor="web"):
            removed += 1

    run_tool = _browser_run_tool(_load_catalog())
    ws.upsert_tool(user_id, "browser", run_tool, enabled=True, actor="web", action="upsert")
    return removed