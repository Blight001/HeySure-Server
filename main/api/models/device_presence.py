"""Live presence snapshot of connected endpoint (desktop / browser) agents.

The in-memory ``agents`` registry only exists in the process that owns the
agent socket server (api-gateway). ai-runtime / mcp-runtime — and even a
separate worker process inside the same container — cannot see it, so they
could not tell which endpoint tools an AI may use. This table mirrors the live
registry into the shared DB so *every* process can resolve and classify
endpoint tools the same way.

One row per logical ``device_id``. ``online`` is flipped on register / disconnect
(and reset on a fresh gateway boot); ``ai_config_id`` tracks the current
Workshop assignment; ``capabilities_json`` is the agent's type-filtered tool
list. Discovery reads ``online`` rows; dispatch still uses the live socket on
the gateway, so a stale ``online`` row at worst offers a tool whose dispatch
then fails gracefully.
"""

import time
from typing import Optional

from sqlmodel import Field, SQLModel


class DevicePresence(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(default=0, index=True)
    device_id: str = Field(index=True)
    ai_config_id: Optional[int] = Field(default=None, index=True)
    # "desktop" | "browser"
    device_type: str = Field(default="")
    # Last-seen display name / platform string reported at register time. Kept
    # so a device that is currently offline can still be listed (and assigned
    # an AI) in the Workshop panel instead of disappearing entirely.
    name: str = Field(default="")
    platform: str = Field(default="")
    # Device-chosen icon URL (normalized at register time): a preset under
    # ``/device_png/N.webp`` or an absolute http(s) URL. Empty = the web falls
    # back to its built-in per-type rendering.
    icon: str = Field(default="")
    # JSON array of the agent's (type-filtered) endpoint tool names.
    capabilities_json: str = Field(default="[]")
    # JSON object mapping each reported tool name to its self-described
    # ``{"description", "input_schema"}``. The agent owns its own tool schemas
    # (browser extension / Windows catalog) and ships them at register time, so
    # the server never hardcodes per-tool schemas. May be ``{}`` for legacy
    # agents that only report names — those fall back to a generic schema.
    tool_defs_json: str = Field(default="{}")
    online: bool = Field(default=True, index=True)
    updated_at: float = Field(default_factory=time.time)
