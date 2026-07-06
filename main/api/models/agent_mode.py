import time
from typing import Optional

from sqlmodel import Field, SQLModel, UniqueConstraint


class AgentMode(SQLModel, table=True):
    """「工作模式」定义（按 AI 隔离）。

    每个模式是一段可注入的前置 prompt：AI 在对话前判断当前工作环境，通过
    ``mode.manage`` 工具切换到对应模式，其 ``prompt`` 会被运行时以
    ``[当前工作模式]`` 段注入系统提示，覆盖上一模式。

    - **每个 AI 一套**：``ai_config_id`` 标识所属 AI，同一 user 下不同 AI 的模式
      清单互相独立（知识库固有人格里按 AI 分别展示与编辑）。
    - ``ai_config_id`` 为 NULL 的行是旧版用户级共享数据，现作为「模板桶」：AI 的
      模式清单首次播种时从这里复制（保留历史编辑），无 AI 上下文的调用也落在这里。
    - ``mode_key`` 在 (user, ai) 范围内唯一，是模式的稳定标识（内置模式为固定 key）。
    - ``is_builtin`` 标记 4 个默认模式（初始对话 / 任务 / 学习 / 修复）：可改 prompt，
      但不可删除；自定义模式可增删改。
    - 「当前使用哪个模式」不落在这里，而是每个 AI 自己的
      ``AssistantAIConfig.current_mode_key``。
    """

    __table_args__ = (
        UniqueConstraint("user_id", "ai_config_id", "mode_key", name="uq_agentmode_user_ai_key"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    # 所属 AI；NULL = 旧版用户级模板桶（见类 docstring）。
    ai_config_id: Optional[int] = Field(default=None, index=True)
    mode_key: str = Field(index=True)
    name: str = Field(default="")
    description: str = Field(default="")
    prompt: str = Field(default="")
    # 模式类型：是否允许调用设备端（桌面 / 浏览器 / 安卓）MCP。
    # False = 对话/纯服务端模式：运行时收走设备端 MCP，前端对话里设备工具组置灰不可勾选。
    # 内置 initial 为 False，task / learning / fix 与自定义模式默认 True。
    allow_device_mcp: bool = Field(default=True)
    is_builtin: bool = Field(default=False)
    sort_order: int = Field(default=100)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
