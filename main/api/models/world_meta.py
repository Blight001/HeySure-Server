"""游戏世界（Agent 进化与实战区域）的成员展示元数据。

刻意独立于 ``AssistantAIConfig`` 主表：皮肤等纯表现层属性不污染业务配置，
删除该表也不影响任何业务链路（见 doc/Agent进化与实战区域设计方案.md §4.1 / §7.4）。
"""

import time
from typing import Optional

from sqlmodel import Field, SQLModel


class WorldActorMeta(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    # 每个 AI 成员最多一行（应用层按 (user_id, ai_config_id) 维护唯一）
    ai_config_id: int = Field(index=True)
    # 外观 JSON，形如 {"skin": "char_member_red.png", "tint": "#ff9aa2",
    # "scale": 1.2, "aura": "#ffd700"}（只存非默认键）；
    # 字段留为 JSON 以便后续继续扩展配件等而无需迁移。
    skin_json: str = Field(default="{}")
    updated_at: float = Field(default_factory=time.time)
