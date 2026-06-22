"""Persistent device → AI binding.

Desktop / browser agents no longer pick an AI themselves; they just log in and
connect. An operator assigns a server-side AI config to each connected device
from the web "作坊" (Workshop) panel. That assignment is stored here, keyed by
the logical ``device_id`` (stable per device) so it survives socket reconnects
and process restarts: on every ``device:register`` the server re-applies the
binding for ``(user_id, device_id)``.

``ai_config_id`` may be NULL — an explicit "unassigned" row is fine, but in
practice unbinding deletes the row.
"""

import time
from typing import Optional

from sqlmodel import Field, SQLModel


class DeviceAiBinding(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    device_id: str = Field(index=True)
    ai_config_id: Optional[int] = Field(default=None, foreign_key="assistantaiconfig.id", index=True)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
