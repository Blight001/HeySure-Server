"""Persistent AI → builtin workshop-agent binding.

The knowledge & evolution workshop (server-builtin, see ``server/library/``)
supports multiple AI members on the same built-in device. 与设备绑定
（``DeviceAiBinding``）的差异仅在绑定方向：内置设备绑定从 AI 侧声明。An AI
with no row cannot see or call any workshop tool.
"""

import time
from typing import Optional

from sqlmodel import Field, SQLModel


class WorkshopAiBinding(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    device_id: str = Field(index=True)
    ai_config_id: int = Field(foreign_key="assistantaiconfig.id", index=True)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
