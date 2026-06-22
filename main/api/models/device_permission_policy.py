"""Per-user, device-type-scoped permission policy for runtime tools.

The device's permission guard (``device/shared/src/runtime/permission-guard.ts``)
maps each permission tag a runtime tool declares to allow / confirm / deny. The
device ships safe defaults; this table lets an operator override them per device
type. The enabled policy rides along in the ``device:tool-config`` push, and the
device applies it via ``setPermissionPolicy`` so governance is centrally owned
(设备端MCP代码下放长期方案 §7.3).

One row per ``(user_id, device_type)``; ``policy_json`` is ``{tag: decision}``.
"""

import time
from typing import Optional

from sqlmodel import Field, SQLModel


class DevicePermissionPolicy(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    device_type: str = Field(default="", index=True)
    # JSON object {permission_tag: "allow"|"confirm"|"deny"}.
    policy_json: str = Field(default="{}")
    updated_at: float = Field(default_factory=time.time)
