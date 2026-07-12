import time
from typing import Optional

from sqlmodel import Field, SQLModel


class AIMessage(SQLModel, table=True):
    """AI 之间的同步消息（带回复语义）。

    生命周期：
      pending   → 已入库等待目标 AI 工作循环捕获
      delivered → 已作为系统提示注入目标 AI 对话，等待 message.send+to+ai 回信或自动回信
      replied   → 目标 AI 已回复，发送方可拿到结果
      timeout   → 超时未回复
      failed    → 目标不存在/不可达
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    message_id: str = Field(index=True, unique=True)  # mai_<uuid>
    user_id: int = Field(foreign_key="user.id", index=True)
    from_ai_config_id: int = Field(foreign_key="assistantaiconfig.id", index=True)
    to_ai_config_id: int = Field(foreign_key="assistantaiconfig.id", index=True)
    # 目标 AI 应该在哪个 session 内消费本消息。发送时绑定，pop 时严格匹配，
    # 防止并发会话相互串话。
    target_session_id: str = Field(default="", index=True)
    # 发送方所在的 session（便于日志/审计/将来回流到原始上下文）。
    from_session_id: str = Field(default="")
    content: str = Field(default="")
    status: str = Field(default="pending", index=True)
    reply_content: Optional[str] = Field(default=None)
    require_reply: bool = Field(default=True)
    timeout_seconds: int = Field(default=120)
    # 消息语义类型，决定收件方注入哪种模板：
    #   inquiry   询问，期望对方明确答复一次
    #   reply     回复，是对某条 inquiry 的答复
    #   chitchat  闲聊，可双向多轮
    #   notify    单向通知，不期望回复
    message_type: str = Field(default="notify", index=True)
    # 在同一条消息链路里的位置：父消息的 cascade_depth + 1；首发消息为 0。
    # 仅用于展示/审计，不作为发送限制。
    cascade_depth: int = Field(default=0)
    delivered_at: Optional[float] = Field(default=None)
    reply_reminded_at: Optional[float] = Field(default=None)
    replied_at: Optional[float] = Field(default=None)
    failure_reason: Optional[str] = Field(default=None)
    created_at: float = Field(default_factory=time.time, index=True)


