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

# Revisions of the Python factory defaults shipped before the Windows runtime
# moved to PowerShell.  ``seed_defaults`` uses these exact fingerprints to
# upgrade untouched factory files without overwriting a user-authored Python
# tool that happens to reuse one of the built-in names.
LEGACY_PYTHON_DEFAULT_REVISIONS = {
    "clipboard.get": "476fd13cfa0b6da890867828e62aecbb99f091546f5f23c639f9655c64398ce4",
    "clipboard.set": "57c3569e48bae468c57e8145baa1dec2df672a2a5a2d1468e3e1643b8729f4a4",
    "display.box": "a1ad87dce58a7766aa7eacfc7d799f62d912418651261c4770d0ee0a23061b3a",
    "fs.list": "c3193153bce09509b9066b5c775f9a0954b3abb5488693d20b167a31fee92360",
    "fs.read": "1223f05231e41eb66b031a4feff794c2d37b2b609b3670a85055e11c2f7b264a",
    "fs.write": "4ff31c095af563197a23719fc894a2dc2e16b68f20e4acbc14f9560b96881871",
    "git.diff": "308ce0723354ff7d5dbd9dbf213f62ea3f0d9a2005a0cf6267d67e0bbaaf3e32",
    "keyboard.press": "771e0dec33868ba352357ad1d698301241b22a88d9787a444d7489075dae8c10",
    "keyboard.type": "377cd392696b072bb5f8b0d6db9cf863fc4eaa9ddae10463c9a1f4d001f9c36d",
    "mouse.click": "e38c76f56e06230f0a374ba33ae401d5cb31abde0b88d6e806a0fcf23e0d1832",
    "mouse.double_click": "c024f6400ba03b8d1280f829e827e2136de532e036eaf12eeb23845cd49bb967",
    "mouse.drag": "5b75eb5166618ae157009e1e3b284f887bea41107d6b5f467202822e71c52baf",
    "mouse.move": "971d5646f6b47e82a86a26bd5e7b2c1bb522234d16d26b2eb50da592f5f2a46e",
    "mouse.right_click": "8749719ba3f9d91b1ad73af40aa7f25dbd5443e27781a053ed725b6ca8e8f0d0",
    "mouse.scroll": "fbf6e9c415dd247c0d76ccd9778f80693490b1fdb7c8b73a7cc37e5f16437857",
    "process.kill": "62688c4221488bfcc1fe9139495af580f90a2851f65fe2b145f7a903f59ae337",
    "process.list": "d2590902bfbad62239c25fb67e0113fe82183e7c98dda68f782f2e4b31935b8e",
    "screen.capture": "54623def419b41da26775f7cf2b01e567791fff74aac33f0d3dbc40bb36eddca",
    "screen.capture_region": "858435a26038197de7ce9e9753ad6d6ae9bc6880f11df91e666ac584f5edd403",
    "screen.info": "e4164d26ec156c674be878c0e14720c715299a85f26d38a8f05aafde9774bec4",
    "speech.speak": "5bda659e24e167fa9b3a14efb940b081e9bad86d69d198ba72274e85a41a6490",
    "text.input": "594f5e3d9668d457f1baac88416e4ea23685bc9e415b6572b8b588ca311e0ad2",
    "ui.click": "0e6c5404ec8f3957a008ad3b903a26695eeeaf991751b5fcc4d799368bbff0ce",
    "ui.inspect": "d0452fea1dc8b323280369c0f471e687bf0cdef715062ede0f901d415b67c870",
    "vision.capture": "cc5cb46e1699f858d5af21adedda132ca00f1d052dc2fce7f40803f654cd2373",
    "vision.capture_mouse": "7ab1bdf4d5ee313bf002355cbaf7772177f5baf25d8c132254596b0ff028b369",
    "window.close": "50149636aa4df0f8be72ad35a5cd0a5107b7efd97723d2d71e1e48a3cd533a57",
    "window.focus": "288edcfd948c044eaae70354759ee1a7ceb19657099bf29427f64a5b110f553a",
    "window.list": "cff6e0db3d4a0f8e7ee9ab7e8b84b661f7c96cb857e590fecb320583da3d6d6a",
}


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
