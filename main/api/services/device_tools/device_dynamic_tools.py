"""Validation for web-authored, device-type-scoped dynamic MCP tools.

The server is the source of truth for these tools; the validation here mirrors
the device-side interpreter (``device/<linux|windows>/src/executor/dynamic.ts``
and ``device/extension/src/lib/tools/dynamic.ts``) so a definition rejected on
the device can never be saved from the web, and vice versa. Keep the rules in
this module and the device ``validate()`` in lockstep.

Storage / CRUD now lives in :mod:`api.services.device_tools.device_workspace_tools` (tools
are persisted as user-workspace files, not DB rows); this module is the shared
validation + constants both that module and the permission policy import.
"""

import hashlib
import json
import re
from typing import Any, Dict, List


# Mirrors NAME_RE in the device interpreters.
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)*$")
# Reserved manager tool names on the devices — a dynamic tool can never shadow
# the editor that loads it.
RESERVED_NAMES = {"mcp.manage_dynamic_tool", "browser_mcp.manage_dynamic_tool"}
VALID_DEVICE_TYPES = ("desktop", "browser", "android")
MAX_CODE_INSTRUCTIONS = 32
# Runtimes the device executors support (runtime/runtime-tool.ts). Keep in
# lockstep with ``isToolRuntime`` on the device.
VALID_RUNTIMES = ("python", "powershell", "shell")
MAX_SOURCE = 64 * 1024
MAX_JS_SOURCE = 64 * 1024
# Tool lifecycle states. ``active`` makes a tool shippable; the rest hold it back.
VALID_STATUSES = ("active", "draft", "disabled", "archived")
# Permission tags the device permission-guard understands (mirror PermissionTag
# in device/shared/src/runtime/permission-guard.ts). Unknown tags are kept (the
# device treats them as confirm-tier) but this drives the policy editor.
KNOWN_PERMISSION_TAGS = (
    "keyboard", "mouse",
    "clipboard.read", "clipboard.write",
    "screen.read",
    "window.read", "window.write",
    "filesystem.read", "filesystem.write",
    "process.read", "process.kill",
    "shell.read", "shell.write",
    "network",
    "browser.dom.read", "browser.dom.write",
)


def normalize_device_type(value: Any) -> str:
    dtype = str(value or "").strip().lower()
    if dtype not in VALID_DEVICE_TYPES:
        raise ValueError(f"device_type must be one of {VALID_DEVICE_TYPES}")
    return dtype


def _revision(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def validate_definition(raw: Any) -> Dict[str, Any]:
    """Validate and normalize a dynamic tool definition.

    Returns ``{name, description, input_schema, code_kind, code, js}``. ``code_kind``
    is ``"js"`` (desktop: a JS function body run with ``(args, cap, ctx)``) or
    ``"program"`` (browser: a call/set/return DSL). Raises ``ValueError`` with a
    human-readable message on any problem (the router maps it to 400).
    """
    if not isinstance(raw, dict):
        raise ValueError("definition must be an object")
    name = str(raw.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise ValueError(f"Invalid dynamic MCP name: {name or '(empty)'}")
    if name in RESERVED_NAMES:
        raise ValueError(f"{name} is reserved")
    description = str(raw.get("description") or "").strip()
    if not description:
        raise ValueError(f"Dynamic MCP {name} requires description")
    input_schema = raw.get("input_schema")
    if input_schema is None:
        input_schema = raw.get("inputSchema")
    if not isinstance(input_schema, dict):
        raise ValueError(f"Dynamic MCP {name} requires input_schema object")

    runtime = str(raw.get("runtime") or "").strip().lower()
    code_kind = str(raw.get("code_kind") or raw.get("codeKind") or "").strip().lower()
    if not code_kind:
        # Infer: a runtime tag means a runtime tool, a non-empty ``js`` means a
        # JS tool, otherwise a program.
        code_kind = "runtime" if runtime else ("js" if str(raw.get("js") or "").strip() else "program")
    if code_kind not in ("js", "program", "runtime"):
        raise ValueError(f"Dynamic MCP {name} code_kind must be 'js', 'program' or 'runtime'")

    if code_kind == "runtime":
        if runtime not in VALID_RUNTIMES:
            raise ValueError(f"Dynamic MCP {name} runtime must be one of {VALID_RUNTIMES}")
        # The runtime body may arrive as ``source`` (preferred) or ``code`` (the
        # device accepts a string ``code`` for runtime tools too).
        source = raw.get("source")
        if not isinstance(source, str) or not source.strip():
            code_field = raw.get("code")
            source = code_field if isinstance(code_field, str) else ""
        if not isinstance(source, str) or not source.strip():
            raise ValueError(f"Dynamic MCP {name} requires non-empty source")
        if len(source) > MAX_SOURCE:
            raise ValueError(f"Dynamic MCP {name} source is too large")
        # Static gate: python tools must at least parse, so a syntax error is
        # caught here on the server instead of failing on every device.
        if runtime == "python":
            try:
                compile(source, f"<{name}>", "exec")
            except SyntaxError as exc:
                raise ValueError(f"Dynamic MCP {name} python source has a syntax error: {exc.msg} (line {exc.lineno})")
        raw_permissions = raw.get("permissions")
        permissions = [
            str(p).strip()
            for p in (raw_permissions if isinstance(raw_permissions, list) else [])
            if str(p).strip()
        ]
        return {
            "name": name,
            "description": description,
            "input_schema": input_schema,
            "code_kind": "runtime",
            "code": [],
            "js": "",
            "runtime": runtime,
            "source": source,
            "permissions": permissions,
        }

    if code_kind == "js":
        js = raw.get("js")
        if not isinstance(js, str) or not js.strip():
            raise ValueError(f"Dynamic MCP {name} requires non-empty js")
        if len(js) > MAX_JS_SOURCE:
            raise ValueError(f"Dynamic MCP {name} js is too large")
        return {
            "name": name,
            "description": description,
            "input_schema": input_schema,
            "code_kind": "js",
            "code": [],
            "js": js,
            "runtime": "",
            "source": "",
            "permissions": [],
        }

    code = raw.get("code")
    if isinstance(code, str):
        try:
            code = json.loads(code)
        except Exception:
            raise ValueError(f"Dynamic MCP {name} code is not valid JSON")
    if not isinstance(code, list) or not code or len(code) > MAX_CODE_INSTRUCTIONS:
        raise ValueError(
            f"Dynamic MCP {name} code must contain 1-{MAX_CODE_INSTRUCTIONS} instructions"
        )
    normalized_code: List[Dict[str, Any]] = []
    for step in code:
        if not isinstance(step, dict) or step.get("op") not in ("call", "set", "return"):
            raise ValueError(f"Invalid instruction in {name}")
        op = step["op"]
        if op == "call" and not str(step.get("tool") or "").strip():
            raise ValueError(f"call instruction in {name} requires tool")
        if op == "set" and not str(step.get("name") or "").strip():
            raise ValueError(f"set instruction in {name} requires name")
        if op == "call" and str(step.get("tool") or "").strip() in RESERVED_NAMES:
            raise ValueError(f"Dynamic MCP code cannot invoke the management tool")
        normalized_code.append(step)

    return {
        "name": name,
        "description": description,
        "input_schema": input_schema,
        "code_kind": "program",
        "code": normalized_code,
        "js": "",
        "runtime": "",
        "source": "",
        "permissions": [],
    }
