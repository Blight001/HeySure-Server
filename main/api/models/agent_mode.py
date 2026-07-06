import time
from typing import Optional

from sqlmodel import Field, SQLModel, UniqueConstraint


class AgentMode(SQLModel, table=True):
    """用户级「工作模式」定义。

    每个模式是一段可注入的前置 prompt：AI 在对话前判断当前工作环境，通过
    ``mode.manage`` 工具切换到对应模式，其 ``prompt`` 会被运行时以
    ``[当前工作模式]`` 段注入系统提示，覆盖上一模式。

    - 用户级共享：同一 user 下的所有 AI 共用这份模式清单（``user_id`` 维度）。
    - ``mode_key`` 在 user 内唯一，是模式的稳定标识（内置模式为固定 key）。
    - ``is_builtin`` 标记 4 个默认模式（普通对话 / 任务 / 学习 / 修复）：可改 prompt，
      但不可删除；自定义模式可增删改。
    - 「当前使用哪个模式」不落在这里，而是每个 AI 自己的
      ``AssistantAIConfig.current_mode_key``。
    """

    __table_args__ = (
        UniqueConstraint("user_id", "mode_key", name="uq_agentmode_user_key"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    mode_key: str = Field(index=True)
    name: str = Field(default="")
    description: str = Field(default="")
    prompt: str = Field(default="")
    is_builtin: bool = Field(default=False)
    sort_order: int = Field(default=100)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
