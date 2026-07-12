from .core import MCPRegistry, MCPTool
from tools.introspection import (
    _mcp_describe_tool,
)
from tools.workspace import (
    _admin_manage,
    _run_command,
)
from tools.tasks import (
    _task_manage,
    TASK_MANAGE_SCHEMA,
)
from tools.task_plan import (
    _todo_manage,
    TODO_MANAGE_SCHEMA,
)
from tools.prompts import (
    _prompt_manage,
    PROMPT_MANAGE_SCHEMA,
)
from tools.agent_mode import (
    _mode_manage,
    MODE_MANAGE_SCHEMA,
)
from tools.communication import (
    _ai_send_message,
    _user_send_message,
)
from tools.conversation import (
    _conversation_manage,
    CONVERSATION_MANAGE_SCHEMA,
)
from tools.knowledge import _knowledge_manage, KNOWLEDGE_MANAGE_SCHEMA
from tools.knowledge_search import _knowledge_search, KNOWLEDGE_SEARCH_SCHEMA
from tools.web_search import _web_search
from tools.device_mcp import _device_mcp_manage, DEVICE_MCP_MANAGE_SCHEMA

def _register_builtin_tools(registry: MCPRegistry) -> None:
    """Populate ``registry`` with all builtin tools.

    Extracted so ``mcp_runtime.mcp.loader`` can rebuild a fresh registry on hot
    reload without needing to ``importlib.reload`` this module (which would
    invalidate references held by callers).
    """
    registry.register(MCPTool(
        name="mcp.describe+tool",
        description=(
            "读取已允许 MCP 工具的完整说明和参数 schema，读取后即可直接调用这些工具。"
            "用 tool 查单个工具；用 tools（数组）一次查多个；用 query 按名称/描述做关键词搜索。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "tool": {"type": "string", "description": "要查看的单个 MCP 工具完整名称。"},
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "一次查看多个工具的完整名称列表。",
                },
                "query": {"type": "string", "description": "关键词，在工具名称和描述中搜索匹配的工具。"},
            },
        },
        handler=_mcp_describe_tool,
    ))

    registry.register(MCPTool(
        name="workspace.search",
        description="联网搜索（基于 Tavily）。当需要对话和工作区里没有的实时或外部信息时使用。",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词。"},
                "search_depth": {
                    "type": "string",
                    "enum": ["basic", "advanced"],
                    "description": "搜索深度：basic=快速，advanced=更深入。默认 advanced。",
                },
                "max_results": {"type": "integer", "description": "返回结果数量，1-20，默认 5。"},
                "include_answer": {"type": "boolean", "description": "是否让 Tavily 附带一段生成的概要答案。"},
                "include_raw_content": {"type": "boolean", "description": "是否在可用时附带网页原始正文。"},
                "include_images": {"type": "boolean", "description": "是否在可用时附带图片结果。"},
            },
            "required": ["query"],
        },
        handler=_web_search,
    ))

    registry.register(MCPTool(
        name="workspace.run+command",
        description=(
            "执行 shell 命令，用于开发或检查工作区。默认在当前 AI 可访问的工作区目录、使用正常进程环境运行。"
            "普通成员只能在自己的 AI 工作区目录内选择 cwd；管理者可使用更宽的用户工作区。"
            "支持显式 shell=cmd/powershell/pwsh，或用 argv + shell=none 绕过 shell 转义。"
            "需要隔离环境时，设置 sandbox_env。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令字符串。默认 shell=auto；Windows 上复杂 PowerShell 请显式 shell=powershell。"},
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "参数化执行，不经过 shell，例如 [\"python\",\"-c\",\"print(1)\"]。传 argv 时可省略 command，shell 固定为 none。",
                },
                "shell": {
                    "type": "string",
                    "enum": ["auto", "cmd", "powershell", "pwsh", "none"],
                    "description": "命令解释器。auto=系统默认 shell；cmd/powershell/pwsh=显式选择；none=仅配合 argv，避免 shell 转义问题。",
                },
                "cwd": {
                    "type": "string",
                    "description": "可选，工作目录。相对路径相对工作区解析；也允许绝对路径。",
                },
                "timeout": {
                    "type": "integer",
                    "description": "可选，超时时间（秒），上限 600，默认 120。",
                },
                "strict_workspace": {
                    "type": "boolean",
                    "description": "为 true 时，拒绝工作区之外的绝对 cwd；普通成员会被强制视为 true。",
                },
                "sandbox_env": {
                    "type": "boolean",
                    "description": "为 true 时，使用工作区内隔离的 HOME/TEMP 目录。默认 false。",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "为 true 时只返回解析后的 cwd/shell/命令，不真正执行。适合删除、覆盖、长命令执行前预检。",
                },
            },
            "required": [],
        },
        handler=_run_command,
        destructive=True,
    ))
    registry.register(MCPTool(
        name="admin.manage",
        description=(
            "管理员/治理统一工具：用 action 选择 overview 获取系统总览（工作区状态 + "
            "已连接端侧 Agent 与受管 AI 配置）/ list_agents 仅列出已连接端侧 Agent 与受管 AI 配置。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["overview", "list_agents"],
                    "description": "overview 系统总览；list_agents 仅列出 Agent。",
                },
            },
            "required": ["action"],
        },
        handler=_admin_manage,
    ))
    registry.register(MCPTool(
        name="task.manage",
        description=(
            "任务管理统一工具（任务=定时/无人值守、在独立会话中运行的后台工作）："
            "用 action 选择 list 列出 / create 创建 / update 接管更新 / delete 删除。"
            "本工具属于图书馆 MCP，调用 AI 必须已绑定图书馆。"
            "绑定后所有身份均可执行全部操作。"
            "复杂长动作用 todo.manage 创建和推进计划。"
        ),
        input_schema=TASK_MANAGE_SCHEMA,
        handler=_task_manage,
        destructive=True,
    ))

    # ---------- todo：统一计划管理（普通对话与任务对话均可用） ----------
    registry.register(MCPTool(
        name="todo.manage",
        description=(
            "统一计划管理工具，只有这一个计划 MCP。action=create 创建/替换分阶段计划；"
            "get 查看当前计划；edit 把当前阶段更新为 completed 或 failed 并自动推进；delete 删除计划。"
            "复杂任务创建计划前先用 knowledge.search 检索历史经验。"
            "最后阶段通过 edit 更新后，系统自动完成总结、日志归档和任务收尾，不需要其它完成工具。"
        ),
        input_schema=TODO_MANAGE_SCHEMA,
        handler=_todo_manage,
        destructive=True,
    ))
    # 与用户通信：把底层机器人投递封装为业务语义上的"给用户发消息"。
    registry.register(MCPTool(
        name="message.send+to+user",
        description=(
            "通过该 AI 已绑定的机器人渠道（飞书或 QQ）给真人用户发送通知。"
            "正常调用只需提供 text 或媒体；不要向用户询问会话 id、openid、target_id 等寻址参数。"
            "QQ 会自动使用当前会话绑定、配置的默认接收目标或最近一次已绑定 QQ 会话；"
            "尚未绑定时工具会返回 delivered=false 和明确原因。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "发给当前已绑定用户的文本；只发媒体时可省略。"},
                "channel": {
                    "type": "string",
                    "enum": ["feishu", "qq"],
                    "description": "兼容性覆盖项；通常不要传，默认自动使用该 AI 绑定的机器人渠道。",
                },
                "receive_id": {"type": "string", "description": "兼容性覆盖项；通知当前绑定用户时不要传，由系统自动解析。"},
                "receive_id_type": {
                    "type": "string",
                    "enum": ["chat_id", "open_id", "user_id", "union_id", "email", "c2c", "group", "channel", "dm"],
                    "description": "兼容性覆盖项；与 receive_id 一起人工指定目标时使用。",
                },
                "media_url": {"type": "string", "description": "图片或视频的 HTTP(S) 链接，服务端拉取后发送。"},
                "media_path": {"type": "string", "description": "服务端本地的图片或视频路径。"},
                "media_type": {"type": "string", "enum": ["image", "video"], "description": "可选，显式指定媒体类型；省略时按 url/path 推断。"},
                "file_name": {"type": "string", "description": "可选，上传媒体时使用的文件名。"},
                "duration": {"type": "integer", "description": "可选，飞书视频上传时的时长（毫秒）。"},
            },
            "required": [],
        },
        handler=_user_send_message,
        destructive=True,
    ))

    registry.register(MCPTool(
        name="conversation.manage",
        description=(
            "会话统一工具：用 action 选择对该 AI 共享对话区的操作——"
            "list 列出会话 / detail 读取会话与消息 / create 新建空白会话 / delete 删除会话 / "
            "rename 改名 / clear 清空消息（默认保留当前这条用户消息）/ compress 压缩当前上下文 / "
            "switch 切换激活会话 / new 新建对话并切换。"
            "清理/压缩当前上下文时不要传 session_id、ai_config_id、ai_kind，运行上下文会自动补齐。"
        ),
        input_schema=CONVERSATION_MANAGE_SCHEMA,
        handler=_conversation_manage,
        destructive=True,
    ))

    registry.register(MCPTool(
        name="knowledge.manage",
        description=(
            "知识库统一工具：用 action 操作图书馆里的传承思想与内置知识类目——"
            "list_thoughts/get_thought/create_thought/edit_thought/delete_thought、install_skill_package、"
            "read_*/update_* 各内置类目。需要该 AI 已绑定图书馆；绑定后所有身份均可读写。"
        ),
        input_schema=KNOWLEDGE_MANAGE_SCHEMA,
        handler=_knowledge_manage,
        destructive=True,
    ))

    registry.register(MCPTool(
        name="knowledge.search",
        description=(
            "语义召回图书馆里的主题思想。根据 query 通过向量检索与关键词回退返回最相关条目，"
            "用于在写作、任务执行和复盘时快速找到可复用的有效思想。"
        ),
        input_schema=KNOWLEDGE_SEARCH_SCHEMA,
        handler=_knowledge_search,
    ))

    # ---------- AI 间通信 ----------
    registry.register(MCPTool(
        name="message.send+to+ai",
        description=(
            "给同一数字社会中的另一个 AI 发消息。目标用 to_ai_config_id（成员 ID）"
            "或 to_ai_name（成员名字）指定，成员名单见系统提示的 [数字社会成员名单] 段。"
            "消息会作为强制系统提示送达；"
            "若目标 AI 正在运行，会中断它当前的运行，并以这条消息打头开启新一轮。"
            "必须指定 message_type，请按语义谨慎选择：\n"
            "- inquiry  ：询问。你在向对方提问、要状态或要结果，通常期望对方答复。\n"
            "- reply    ：回复。你在答复对方先前发来的 inquiry；应带 reply_to_message_id。\n"
            "- notify   ：通知。单向状态、结果或提醒，不期待对方回复。\n"
            "- chitchat ：闲聊，可双向多轮。\n"
            "默认排队后即返回；只有调用方确实需要同步等待答复时才设 require_reply=true。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "to_ai_config_id": {
                    "type": "integer",
                    "description": "目标 AI 的 ai_config_id（与 to_ai_name 二选一，见系统提示 [数字社会成员名单]）。",
                },
                "to_ai_name": {
                    "type": "string",
                    "description": "目标 AI 的名字（与 to_ai_config_id 二选一）；服务端按名字精确匹配解析。",
                },
                "content": {"type": "string", "description": "消息正文。"},
                "message_type": {
                    "type": "string",
                    "enum": ["inquiry", "reply", "chitchat", "notify"],
                    "description": (
                        "必填，决定送达提示里的语义：inquiry=询问/需要答复，"
                        "reply=回复上一条 inquiry，notify=单向通知/不期待回复，chitchat=闲聊。"
                    ),
                },
                "require_reply": {
                    "type": "boolean",
                    "description": (
                        "默认 false，仅控制本次调用是否同步等待，不能替代必填的 message_type。"
                        "常规 AI 协作请保持 false，对方的答复会作为新的 message.send+to+ai 调用回来。"
                    ),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "可选，require_reply=true 时的最长等待秒数。省略则用默认长等待（86400 秒/24 小时）；确实想等更久才调大。",
                },
                "reply_to_message_id": {
                    "type": "string",
                    "description": "可选，当本次是回复时，传入对方原消息 id（mai_...），便于服务端维持消息线程上下文。",
                },
                "current_session_id": {
                    "type": "string",
                    "description": "可选，当前对话/会话 id；省略时运行时会自动补上。",
                },
            },
            "required": ["content", "message_type"],
        },
        handler=_ai_send_message,
        destructive=True,
    ))

    registry.register(MCPTool(
        name="prompt.manage",
        description=(
            "Prompt 统一工具：用 action 选择 list_targets 列目标 / read_ai 读 AI 人格 prompt / "
            "write_ai 改 AI 人格 prompt / read_system 读系统 prompt / write_system 改系统 prompt。"
            "需要该 AI 已绑定图书馆，绑定后不区分身份。prompt 正文存放在 KnowledgeBase 的 md 文件里。"
        ),
        input_schema=PROMPT_MANAGE_SCHEMA,
        handler=_prompt_manage,
        destructive=True,
    ))

    registry.register(MCPTool(
        name="mode.manage",
        description=(
            "工作模式统一工具：AI 对话前先判断当前工作环境，再切换到对应模式。"
            "默认的 initial 初始对话模式「不在工作房间」——只有基础对话工具，看不到设备 / 工作 MCP；"
            "切到 task / learning 等工作模式，系统才把设备 MCP 交回。切换只在工具结果里返回该模式说明，"
            "不改写人格 / 系统提示。用 action 选择 list 列出所有模式 / get 读取某模式 prompt / create 创建自定义模式 / "
            "update 修改模式 / delete 删除自定义模式 / use 切换当前 AI 到某模式。"
            "模式清单按 AI 隔离：你增删改查到的是自己的模式，不影响其他 AI。"
            "每个模式带类型 allow_device_mcp：false 的模式收走设备端（桌面/浏览器/安卓）MCP；"
            "切到 true 的模式时，use 结果结尾会自动附带当前可用的设备端 MCP 工具说明。"
            "内置 3 种：initial 初始对话（默认，只聊天、无设备工具）/ task 任务 / learning 学习（可改 prompt，不可删）。"
        ),
        input_schema=MODE_MANAGE_SCHEMA,
        handler=_mode_manage,
        destructive=True,
    ))

    registry.register(MCPTool(
        name="device+mcp.manage",
        description=(
            "自主管理设备端 MCP 工具（按设备类型 desktop/browser），可用于迭代更好用的工具实现。"
            "list/get 查看；capabilities 列出该类型设备可调用的原生能力；upsert 创建或覆盖；delete 删除。"
            "desktop 工具是在设备上运行的 JS（作用域有 args/cap/ctx，cap 是原生能力库，如 cap.call('fs.read', args)）；"
            "browser 工具用 call/set/return 指令。保存后立即下发到在线设备，下一轮即可调用。"
        ),
        input_schema=DEVICE_MCP_MANAGE_SCHEMA,
        handler=_device_mcp_manage,
        destructive=True,
    ))

registry = MCPRegistry()
_register_builtin_tools(registry)
