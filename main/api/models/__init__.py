"""SQLModel tables and Pydantic schemas, grouped by domain.

The public surface kept stable: callers continue to do
``from api.models import User, ChatMessage, ...``. Each domain lives in its
own sub-module so editing one set of tables (e.g. chat) does not force
re-reading hundreds of lines of unrelated definitions.

Sub-modules:
- defaults     — prompt template / UI default constants
- user         — User account + auth schemas
- chat         — ChatMessage / ChatSession / ChatRun
- ai_config    — AssistantAIConfig (per-AI configuration)
- ai_runtime   — AITaskJob / AIRuntimeStatus / TokenUsageSnapshot
- project      — EvolutionProject (multi-AI collaboration containers)
- knowledge    — Memory (KnowledgeEntry table removed; knowledge is file-based)
- communication — AIMessage
"""

from .ai_config import (
    AssistantAIConfig,
    AssistantAIConfigCreate,
    AssistantAIConfigUpdate,
)
from .agent_mode import AgentMode
from .ai_runtime import (
    AgentDispatchTask,
    AIRuntimeStatus,
    AITaskJob,
    TaskPhase,
    TaskPlan,
    TokenUsageSnapshot,
)
from .chat import (
    ChatMessage,
    ChatMessageCreate,
    ChatMessageMedia,
    ChatMessageUpdate,
    ChatRun,
    ChatSession,
    ChatSessionCreate,
)
from .communication import AIMessage
from .defaults import (
    CHITCHAT_MAX_DEPTH,
    DEFAULT_AI_MESSAGE_CHITCHAT_TEMPLATE,
    DEFAULT_AI_MESSAGE_INQUIRY_TEMPLATE,
    DEFAULT_AI_MESSAGE_INQUIRY_REMINDER,
    DEFAULT_AI_MESSAGE_NOTIFY_TEMPLATE,
    DEFAULT_AI_MESSAGE_REPLY_SUCCESS,
    DEFAULT_AI_MESSAGE_REPLY_TEMPLATE,
    DEFAULT_COMPRESSION_PROMPT,
    DEFAULT_MCP_CALL_METHOD,
    DEFAULT_MCP_FORMAT_ERROR_HINT,
    DEFAULT_MCP_NAMESPACE_HINTS,
    DEFAULT_RESUME_TASK_PROMPT,
    DEFAULT_START_TASK_PROMPT,
    DEFAULT_SUPERVISION_PROMPT,
    DEFAULT_TASK_PLAN_FLOW_PROMPT,
    DEFAULT_UI_FONT_SIZE,
    DEFAULT_UI_THEME_MODE,
    DEFAULT_USER_MESSAGE_NOTICE,
)
from .admin_audit import AdminAuditLog
from .device_binding import DeviceAiBinding
from .device_mcp_permission import DeviceTypeMcpPermission
from .device_presence import DevicePresence
from .device_dynamic_tool import DeviceDynamicTool
from .device_dynamic_tool_version import DeviceDynamicToolVersion
from .device_permission_policy import DevicePermissionPolicy
from .mcp_call_stat import McpToolStat, McpFailureEvent
from .bot_session_route import BotSessionRoute, BotUserCursor
from .workshop_binding import WorkshopAiBinding
from .knowledge import KnowledgeEntry, Memory
from .project import EvolutionProject, EvolutionProjectCreate, EvolutionProjectUpdate
from .system import EmailVerificationCode, SystemSetting
from .user import Token, User, UserCreate, UserLogin, UserRead, UserUpdate
from .world_meta import WorldActorMeta

__all__ = [
    # defaults
    "CHITCHAT_MAX_DEPTH",
    "DEFAULT_AI_MESSAGE_CHITCHAT_TEMPLATE",
    "DEFAULT_AI_MESSAGE_INQUIRY_TEMPLATE",
    "DEFAULT_AI_MESSAGE_INQUIRY_REMINDER",
    "DEFAULT_AI_MESSAGE_NOTIFY_TEMPLATE",
    "DEFAULT_AI_MESSAGE_REPLY_SUCCESS",
    "DEFAULT_AI_MESSAGE_REPLY_TEMPLATE",
    "DEFAULT_COMPRESSION_PROMPT",
    "DEFAULT_MCP_CALL_METHOD",
    "DEFAULT_MCP_FORMAT_ERROR_HINT",
    "DEFAULT_MCP_NAMESPACE_HINTS",
    "DEFAULT_RESUME_TASK_PROMPT",
    "DEFAULT_START_TASK_PROMPT",
    "DEFAULT_SUPERVISION_PROMPT",
    "DEFAULT_TASK_PLAN_FLOW_PROMPT",
    "DEFAULT_UI_FONT_SIZE",
    "DEFAULT_UI_THEME_MODE",
    "DEFAULT_USER_MESSAGE_NOTICE",
    # user
    "Token",
    "User",
    "UserCreate",
    "UserLogin",
    "UserRead",
    "UserUpdate",
    # admin
    "AdminAuditLog",
    # system-wide settings + email verification
    "SystemSetting",
    "EmailVerificationCode",
    # agent device bindings
    "DeviceAiBinding",
    "DeviceTypeMcpPermission",
    "DevicePresence",
    "DeviceDynamicTool",
    "DeviceDynamicToolVersion",
    "DevicePermissionPolicy",
    "McpToolStat",
    "McpFailureEvent",
    "WorkshopAiBinding",
    # chat
    "ChatMessage",
    "ChatMessageCreate",
    "ChatMessageMedia",
    "ChatMessageUpdate",
    "ChatRun",
    "ChatSession",
    "ChatSessionCreate",
    # ai config + runtime
    "AssistantAIConfig",
    "AssistantAIConfigCreate",
    "AssistantAIConfigUpdate",
    "AgentMode",
    "AgentDispatchTask",
    "AIRuntimeStatus",
    "AITaskJob",
    "TaskPlan",
    "TaskPhase",
    "TokenUsageSnapshot",
    # project
    "EvolutionProject",
    "EvolutionProjectCreate",
    "EvolutionProjectUpdate",
    # knowledge
    # "KnowledgeEntry",  # table removed from active use; definition kept for migration tools only
    "Memory",
    "WorldActorMeta",
    # communication
    "AIMessage",
    # bots (unified session route, channel-keyed)
    "BotSessionRoute",
    "BotUserCursor",
]
