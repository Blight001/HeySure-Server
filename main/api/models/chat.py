import time
from typing import Optional

from sqlalchemy import Column, ForeignKey, Integer, LargeBinary, Text
from sqlmodel import Field, SQLModel


class ChatMessage(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    ai_config_id: Optional[int] = Field(default=None, index=True)
    ai_kind: str = Field(default="assistant", index=True)  # assistant / core
    session_id: str = Field(default="default", index=True)  # 会话 ID
    session_name: Optional[str] = Field(default=None)  # 会话名称（通常是第一条消息）
    role: str  # "user" or "assistant"
    content: str
    think: Optional[str] = None
    tags: str = Field(default="")  # 逗号分隔的标签

    # Token 使用量和模型信息
    model: Optional[str] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    system_prompt: Optional[str] = None
    finish_reason: Optional[str] = None
    latency: Optional[float] = None  # 延迟，单位秒

    created_at: float = Field(default_factory=time.time)


class ChatMessageCreate(SQLModel):
    role: str
    content: str
    ai_config_id: Optional[int] = None
    ai_kind: str = "assistant"
    session_id: Optional[str] = "default"
    session_name: Optional[str] = None
    think: Optional[str] = None
    tags: Optional[str] = ""

    model: Optional[str] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    system_prompt: Optional[str] = None
    finish_reason: Optional[str] = None
    latency: Optional[float] = None


class ChatMessageUpdate(SQLModel):
    tags: Optional[str] = None


class ChatMessageMedia(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    message_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("chatmessage.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    user_id: int = Field(foreign_key="user.id", index=True)
    media_type: str = Field(default="image/png")
    token: str = Field(index=True)
    data: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    bytes: int = Field(default=0)
    created_at: float = Field(default_factory=time.time)


class ChatSession(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    ai_config_id: Optional[int] = Field(default=None, index=True)
    ai_kind: str = Field(default="assistant", index=True)
    session_id: str = Field(index=True)
    session_name: str
    # Per-conversation switch: when true, assistant replies in this conversation
    # are forwarded to the AI's bound bot default receiver. Defaults off; bot-
    # originated conversations (qq/feishu) reply through their own routes and do
    # not depend on this flag.
    forward_to_bot: bool = Field(default=False)
    # Optional per-conversation model preset. Empty means "follow the AI's
    # configured default"; bot /models commands set only this conversation.
    model_preset_id: str = Field(default="", index=True)
    # JSON map: tool name -> {schema_version, described_at}. This is deliberately
    # session-scoped so a clarification reply can continue with tools whose
    # schemas were already loaded in an earlier inference run.
    described_tools_json: str = Field(
        default="",
        sa_column=Column(Text, nullable=False, server_default=""),
    )
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class ChatSessionCreate(SQLModel):
    ai_config_id: Optional[int] = None
    ai_kind: str = "assistant"
    session_name: str


class ChatRun(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: str = Field(index=True, unique=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    ai_config_id: Optional[int] = Field(default=None, index=True)
    ai_kind: str = Field(default="assistant", index=True)
    session_id: str = Field(default="default", index=True)
    session_name: Optional[str] = None
    status: str = Field(default="queued", index=True)  # queued/running/completed/error/stopped
    stop_requested: bool = Field(default=False)
    error_message: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    # Updated by the worker every few seconds while a run is running. The
    # watchdog in api-gateway marks rows with status='running' and a stale
    # heartbeat as 'error' so dead workers don't leave ghost runs.
    heartbeat_at: Optional[float] = None
    # JSON-encoded kwargs that the dispatcher (start_chat_run / Feishu /
    # scheduler) wanted to hand to _run_worker — e.g. ``merged_system_prompt``
    # built with Feishu-runtime guidance, or ``max_steps`` overridden for a
    # particular task. In remote dispatch mode ai-runtime reads this column
    # so it can rebuild the worker call identically to what the dispatcher
    # intended. Empty / NULL means "use defaults".
    worker_kwargs_json: Optional[str] = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
