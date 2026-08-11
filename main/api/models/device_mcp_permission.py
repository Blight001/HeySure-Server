"""Per-agent endpoint MCP permission scope.

Endpoint agents (Linux / desktop / browser) advertise their full tool surface
in the ``capabilities`` array of ``device:register``. Which of those tools the
agent's bound AI may actually drive is stored here, keyed by ``(user_id,
device_id, ai_config_id)``. A ``NULL`` AI id is the device default template;
bound members store independent scopes.

``tools_json`` is a JSON array of allowed tool names; no row means "closed"
→ the bound AI may not use tools from that agent until the Workshop saves a
scope (see ``connector_runtime.dispatch.desktop_device_tools``). ``ai_config_id`` and
``device_type`` is informational; ``ai_config_id`` is part of the logical key.

The class name ``DeviceTypeMcpPermission`` and table are retained from the
earlier per-type model to avoid a destructive rename; the keying is now
per-agent.
"""

import time
from typing import Optional

from sqlmodel import Field, SQLModel


class DeviceTypeMcpPermission(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    # Stable logical agent id (from device:register). The scope key.
    device_id: str = Field(default="", index=True)
    # NULL is the device template; non-NULL rows are per-member scopes.
    ai_config_id: Optional[int] = Field(default=None, index=True)
    # Informational: "linux" | "desktop" | "browser".
    device_type: str = Field(default="", index=True)
    # JSON-encoded list of allowed endpoint tool names.
    tools_json: str = Field(default="[]")
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
