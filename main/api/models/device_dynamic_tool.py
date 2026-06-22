"""Web-authored dynamic MCP tools, scoped by device type.

The device shells (Linux / Windows desktop + browser extension) already ship a
"dynamic MCP" interpreter: JSON programs (``call`` / ``set`` / ``return``
instructions) that can be hot-loaded without recompiling the client and may
wrap built-in primitives via ``builtin:<name>``. Until now those definitions
lived only on the device (authored by the AI through
``mcp.manage_dynamic_tool``).

This table inverts the source of truth: an operator authors dynamic tools in
the web console, the server stores them here keyed by ``(user_id,
device_type, name)``, and pushes the enabled set down to every online device of
that type (``device:tool-config``). The device merges them into its dynamic
interpreter and re-reports its tool catalog, so the rest of the endpoint
pipeline (presence snapshot, per-agent scope, dispatch) keeps working unchanged.

``device_type`` is ``"desktop"`` or ``"browser"`` — every device of that kind
owned by the user shares the same dynamic tool set, so a tool change ships to
all of them at once and the client never needs a new release for it.
"""

import time
from typing import Optional

from sqlmodel import Field, SQLModel


class DeviceDynamicTool(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    # "desktop" | "browser" — the device kind this tool ships to.
    device_type: str = Field(default="", index=True)
    # Dynamic MCP tool name (NAME_RE-validated). May match a built-in to
    # override it on the device via ``builtin:<name>``.
    name: str = Field(default="", index=True)
    description: str = Field(default="")
    # JSON object: the tool's input JSON Schema.
    input_schema_json: str = Field(default="{}")
    # "program" → code_json holds a call/set/return program (browser / safe DSL).
    # "js"      → js_source holds a JS function body run by the desktop runtime
    #             with (args, cap, ctx) in scope. Desktop tools default to "js"
    #             so the whole implementation lives on the server.
    # "runtime" → source holds a plain script run by a device runtime (python /
    #             powershell / shell), see ``runtime`` below.
    code_kind: str = Field(default="program")
    # JSON array: the call/set/return program (1-32 instructions). Used when
    # code_kind == "program".
    code_json: str = Field(default="[]")
    # JS function body. Used when code_kind == "js".
    js_source: str = Field(default="")
    # Device runtime for code_kind == "runtime": "python" | "powershell" | "shell".
    runtime: str = Field(default="")
    # The runtime tool body (python/powershell script or shell command). Used
    # when code_kind == "runtime".
    source: str = Field(default="")
    # JSON array of permission tags the runtime tool declares; the device checks
    # them locally (allow/confirm/deny) before running.
    permissions_json: str = Field(default="[]")
    # Disabled tools are kept for editing but never shipped to devices.
    enabled: bool = Field(default=True)
    # Approval lifecycle (设备端MCP代码下放长期方案 §7.3 / 阶段五):
    # "active" (approved, shippable) | "draft" (AI-submitted, awaiting human
    # approval) | "disabled" | "archived". AI-authored upserts land as "draft";
    # an operator approves them to "active". Only active+enabled tools ship.
    status: str = Field(default="active", index=True)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
