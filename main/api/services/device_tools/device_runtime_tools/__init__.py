"""Factory-default desktop runtime tools, shipped as standalone .ps1 bodies.

These are the read-only "出厂默认" set. On first use they are seeded into each
user's workspace (``<workspace>/device_tools/desktop/``) where they become the
editable source of truth — the AI manages them as files via MCP, not the DB.

Each tool is one ``bodies/<name>.ps1`` (the PowerShell body, reading the
injected ``$toolArgs`` object and assigning ``$result``; executed by the
device-side powershell-runner, Windows PowerShell 5.1 compatible) plus an
entry in ``definitions.json`` (metadata: name / description / input_schema /
permissions). All defaults are ``runtime=powershell`` except ``shell.run``
(``runtime=shell``); the python runtime remains available for user-authored
tools but is no longer the factory base.
"""

import json
import os
from typing import Any, Dict, List

_DIR = os.path.dirname(__file__)
_BODIES = os.path.join(_DIR, "bodies")


def load_default_tools() -> List[Dict[str, Any]]:
    with open(os.path.join(_DIR, "definitions.json"), encoding="utf-8") as f:
        defs = json.load(f)
    out: List[Dict[str, Any]] = []
    for d in defs:
        with open(os.path.join(_BODIES, d["file"]), encoding="utf-8") as bf:
            source = bf.read()
        out.append({
            "name": d["name"],
            "description": d["description"],
            "input_schema": d["input_schema"],
            "code_kind": "runtime",
            "runtime": d.get("runtime", "powershell"),
            "source": source,
            "code": [],
            "js": "",
            "permissions": d.get("permissions", []),
        })
    return out
