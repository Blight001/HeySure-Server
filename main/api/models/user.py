import time
from typing import Optional

from sqlmodel import Field, SQLModel

from .defaults import (
    DEFAULT_MODEL_PRESETS,
    DEFAULT_UI_BRAIN_VIEW_MODE,
    DEFAULT_UI_FONT_SIZE,
    DEFAULT_UI_THEME_MODE,
)


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    account: str = Field(unique=True, index=True)
    hashed_password: str
    avatar: Optional[str] = None
    # 邮箱（经验证码验证后绑定）。注册模式为 email 时必填且唯一；
    # 唯一性在应用层校验（SQLite ALTER TABLE 无法补加 UNIQUE 约束）。
    email: Optional[str] = Field(default=None, index=True)
    # Platform-level access tier surfaced by the admin panel:
    # ``owner`` (房主) > ``admin`` (管理员) > ``member`` (成员). The first
    # registered user is bootstrapped to ``owner``; everyone else defaults
    # to ``member`` until an owner/admin promotes them.
    role: str = Field(default="member", index=True)
    created_at: float = Field(default_factory=time.time)

    # 主脑 AI 配置
    admin_api_key: str = Field(default="sk-cb40bc0b0b894934919907913e337927")
    admin_base_url: str = Field(default="https://api.deepseek.com/chat/completions")
    admin_model: str = Field(default="deepseek-chat")
    # 系统提示词文本（admin_prompt / mcp_call_method / mcp_namespace_hints /
    # mcp_dynamic_rule / mcp_format_error_hint / default_*_prompt /
    # default_compression_prompt / prompt_ai_message_* / prompt_user_message_notice）
    # 已迁出数据库，真相源为 KnowledgeBase/system/*.md（见 api.services.knowledge.kb_store）。
    # 这些数值/配置项不是提示词，保留在库。
    mcp_max_steps: int = Field(default=48)
    # Model-context policy for MCP records from earlier runs in the same chat.
    # The original ChatMessage rows stay untouched; only the replayed tool-result
    # body is shortened to this limit.
    # Compaction is kept as a safety valve for pathologically large single tool
    # results (e.g. a full-page browser_observe dump); the default cap is generous
    # so ordinary tool outputs (read file / search / command) replay in full across
    # turns — mainstream full-fidelity behaviour. Prefix stays deterministic, so
    # server-side automatic prefix caching (DeepSeek/OpenAI/Grok) still hits.
    mcp_history_compaction_enabled: bool = Field(default=True)
    mcp_history_result_max_chars: int = Field(default=8000)
    conversation_auto_compress_enabled: bool = Field(default=True)
    # Per-role MCP allow-list configured by the admin. JSON object mapping a role
    # tier (assistant_admin / digital_member_manager / digital_member_member) to a
    # list of allowed tool names. Empty string means "use the per-role default".
    role_mcp_permissions: str = Field(default="")
    tavily_api_key: str = Field(default="")
    model_presets: str = Field(default=DEFAULT_MODEL_PRESETS)

    # 默认任务提示词（start/resume/supervision/inheritance）已迁至
    # KnowledgeBase/system/*.md；此处仅保留数值设置。
    default_supervision_idle_seconds: int = Field(default=25)

    # AI 通信提示词模板已迁至 KnowledgeBase/system/*.md；此处仅保留数值设置。
    ai_message_inquiry_reminder_seconds: int = Field(default=3)

    ui_theme_mode: str = Field(default=DEFAULT_UI_THEME_MODE)
    ui_font_size: str = Field(default=DEFAULT_UI_FONT_SIZE)
    ui_brain_view_mode: str = Field(default=DEFAULT_UI_BRAIN_VIEW_MODE)


class UserCreate(SQLModel):
    name: str
    account: str
    password: str
    avatar: Optional[str] = None
    # 注册模式为 email 时必填：邮箱 + 已发送到该邮箱的验证码
    email: Optional[str] = None
    email_code: Optional[str] = None


class UserLogin(SQLModel):
    account: str
    password: str


class UserRead(SQLModel):
    id: int
    name: str
    account: str
    avatar: Optional[str] = None
    email: Optional[str] = None
    role: str = "member"
    admin_api_key: str
    admin_base_url: str
    admin_model: str
    admin_prompt: str
    mcp_call_method: str
    mcp_namespace_hints: str
    mcp_dynamic_rule: str
    mcp_format_error_hint: str
    mcp_max_steps: int
    mcp_history_compaction_enabled: bool = True
    mcp_history_result_max_chars: int = 8000
    conversation_auto_compress_enabled: bool = True
    role_mcp_permissions: str
    tavily_api_key: str
    model_presets: str
    default_start_task_prompt: str
    default_resume_task_prompt: str
    default_supervision_prompt: str
    default_supervision_idle_seconds: int
    default_compression_prompt: str
    prompt_ai_message_notify: str
    prompt_ai_message_inquiry: str
    ai_message_inquiry_reminder_seconds: int
    prompt_ai_message_inquiry_reminder: str
    prompt_ai_message_reply: str
    prompt_ai_message_chitchat: str
    prompt_ai_message_reply_success: str
    prompt_user_message_notice: str
    ui_theme_mode: str
    ui_font_size: str
    ui_brain_view_mode: str


class UserUpdate(SQLModel):
    name: Optional[str] = None
    password: Optional[str] = None
    avatar: Optional[str] = None
    admin_api_key: Optional[str] = None
    admin_base_url: Optional[str] = None
    admin_model: Optional[str] = None
    admin_prompt: Optional[str] = None
    mcp_call_method: Optional[str] = None
    mcp_namespace_hints: Optional[str] = None
    mcp_dynamic_rule: Optional[str] = None
    mcp_format_error_hint: Optional[str] = None
    mcp_max_steps: Optional[int] = None
    mcp_history_compaction_enabled: Optional[bool] = None
    mcp_history_result_max_chars: Optional[int] = None
    conversation_auto_compress_enabled: Optional[bool] = None
    role_mcp_permissions: Optional[str] = None
    tavily_api_key: Optional[str] = None
    model_presets: Optional[str] = None
    default_start_task_prompt: Optional[str] = None
    default_resume_task_prompt: Optional[str] = None
    default_supervision_prompt: Optional[str] = None
    default_supervision_idle_seconds: Optional[int] = None
    default_compression_prompt: Optional[str] = None
    prompt_ai_message_notify: Optional[str] = None
    prompt_ai_message_inquiry: Optional[str] = None
    ai_message_inquiry_reminder_seconds: Optional[int] = None
    prompt_ai_message_inquiry_reminder: Optional[str] = None
    prompt_ai_message_reply: Optional[str] = None
    prompt_ai_message_chitchat: Optional[str] = None
    prompt_ai_message_reply_success: Optional[str] = None
    prompt_user_message_notice: Optional[str] = None
    ui_theme_mode: Optional[str] = None
    ui_font_size: Optional[str] = None
    ui_brain_view_mode: Optional[str] = None


class Token(SQLModel):
    access_token: str
    token_type: str
    user: UserRead
    agent_socket_url: str = ""
    # WebRTC ICE servers (STUN + optional TURN) for remote control, resolved
    # server-side from admin config / env so every client shares one source.
    ice_servers: list = []
