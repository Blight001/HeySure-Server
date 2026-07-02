from .core import MCPRegistry, MCPTool
from tools.introspection import (
    _mcp_describe_tool,
)
from tools.workspace import (
    _admin_manage,
    _file_manage,
    _run_command,
    FILE_MANAGE_SCHEMA,
)
from tools.tasks import (
    _task_manage,
    TASK_MANAGE_SCHEMA,
)
from tools.task_plan import (
    _phase_complete,
    _plan_create,
    _plan_finish,
)
from tools.prompts import (
    _prompt_manage,
    PROMPT_MANAGE_SCHEMA,
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
        name="mcp.describe_tool",
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
        name="workspace.run_command",
        description=(
            "执行 shell 命令，用于开发或检查工作区。默认在当前用户的工作区目录、使用正常进程环境运行，"
            "允许绝对路径和环境变量。支持显式 shell=cmd/powershell/pwsh，或用 argv + shell=none 绕过 shell 转义。"
            "需要隔离、只在工作区内运行时，设置 strict_workspace 或 sandbox_env。"
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
                    "description": "为 true 时，拒绝工作区之外的绝对 cwd。默认 false。",
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
        name="workspace.manage",
        description=(
            "工作区文件统一工具：用 action 选择 read 读取 / tree 列出文件树 / write 创建覆盖 / edit 按块编辑。"
            "路径必须位于当前 AI 工作区内；write/edit 需管理者及以上。"
            "（运行命令用 workspace.run_command，联网搜索用 workspace.search。）"
        ),
        input_schema=FILE_MANAGE_SCHEMA,
        handler=_file_manage,
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
            "create/update/delete 需管理者及以上。"
            "对长动作做分阶段执行用 plan 域：plan.create / plan.phase_complete / plan.finish。"
        ),
        input_schema=TASK_MANAGE_SCHEMA,
        handler=_task_manage,
        destructive=True,
    ))

    # ---------- plan 域：分阶段执行（长动作；普通对话与任务对话均可用） ----------
    registry.register(MCPTool(
        name="plan.create",
        description=(
            "为复杂的长动作制定一份完整计划，行动前先调用（普通对话和任务对话都可使用）。"
            "对于实际的多阶段操作任务，**请先调用 knowledge.search（或 librarian.consult，若已绑定）检索知识库中相关历史经验与流程**，"
            "再基于检索结果拆分阶段。"
            "把整体目标拆成有序的多个阶段，"
            "每个阶段有明确的目标(goal)与结束标志(done_signal)，并可在 actions 里列出该阶段的子行动"
            "（每个子行动也有自己的 goal 与 done_signal）。"
            "登记后从第 1 个阶段开始执行：每完成一个阶段调用 plan.phase_complete；"
            "完成最后一个阶段时，系统会自动收尾整个计划并归档，无需再单独调用收尾工具。"
            "同一会话只保留一份进行中的计划，重复调用会覆盖旧计划。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "整个任务的总体目标，一句话讲清要交付什么。"},
                "phases": {
                    "type": "array",
                    "description": "有序的阶段列表（建议 2-20 个）。阶段太少说明任务不需要计划，太多说明拆得过细。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "阶段名称。"},
                            "goal": {"type": "string", "description": "该阶段要达成的明确目标。"},
                            "done_signal": {"type": "string", "description": "判断该阶段已完成的明确结束标志。"},
                            "actions": {
                                "type": "array",
                                "description": "该阶段的子行动列表，每个子行动有自己的 goal 与 done_signal。",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "goal": {"type": "string", "description": "子行动的目标。"},
                                        "done_signal": {"type": "string", "description": "子行动的结束标志。"},
                                    },
                                    "required": ["goal"],
                                },
                            },
                        },
                        "required": ["goal", "done_signal"],
                    },
                },
            },
            "required": ["goal", "phases"],
        },
        handler=_plan_create,
        destructive=True,
    ))
    registry.register(MCPTool(
        name="plan.phase_complete",
        description=(
            "plan 的子操作：完成当前阶段并收尾（无需总结）。调用后系统会自动隐藏上一阶段的深度思考与 MCP "
            "详细结果、只保留调用状态，并自动下发下一个阶段；若已是最后一个阶段，系统会自动收尾整个计划"
            "并归档，无需你再调用其它收尾工具。若该阶段未达成目标，可传 status=failed 如实记录后继续。"
            "summary 可选，一般留空即可。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["completed", "failed"],
                    "description": "阶段结果，默认 completed；未达成目标用 failed。",
                },
                "summary": {"type": "string", "description": "可选，一句话备注该阶段；留空即可。"},
            },
        },
        handler=_phase_complete,
        destructive=True,
    ))
    registry.register(MCPTool(
        name="plan.finish",
        description=(
            "手动收尾整个计划（可选，正常情况下无需调用）。完成最后一个阶段（plan.phase_complete）时"
            "系统会自动收尾并归档；本工具仅作为需要亲自给出完整复盘时的备用手段，且要求所有阶段均已收尾。"
            "系统会隐藏全过程的深度思考与 MCP 详细结果，把完整行动流程写入工作区的成功/失败日志"
            "（logs/success 或 logs/failure）。outcome=success 写成功日志，failure 写失败日志。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "outcome": {
                    "type": "string",
                    "enum": ["success", "failure"],
                    "description": "整个任务的最终结果：success=成功，failure=失败。",
                },
                "summary": {
                    "type": "string",
                    "minLength": 1,
                    "description": "整个任务的完整复盘总结：目标、过程、产出/失败原因、可复用经验。",
                },
            },
            "required": ["outcome", "summary"],
        },
        handler=_plan_finish,
        destructive=True,
    ))
    # 与用户通信：把底层机器人投递封装为业务语义上的"给用户发消息"。
    registry.register(MCPTool(
        name="message.send_to_user",
        description=(
            "通过绑定的机器人渠道（飞书或 QQ）给真人用户发文本消息。"
            "用于主动通知、状态更新，或异步请用户去做某事。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "发给用户的文本；只发媒体时可省略。"},
                "channel": {
                    "type": "string",
                    "enum": ["feishu", "qq"],
                    "description": "发送渠道；默认用该 AI 配置的机器人渠道。",
                },
                "receive_id": {"type": "string", "description": "可选，接收者 id；默认用 AI 配置里的默认接收者。"},
                "receive_id_type": {
                    "type": "string",
                    "enum": ["chat_id", "open_id", "user_id", "union_id", "email", "c2c", "group", "channel", "dm"],
                    "description": "接收者 id 类型；QQ 用 c2c/group/channel/dm。",
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
            "read_*/update_* 各内置类目。需要该 AI 已绑定图书馆；写操作按角色受限。"
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
        name="message.send_to_ai",
        description=(
            "给同一数字社会中的另一个 AI 发消息。消息会作为强制系统提示送达；"
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
                "to_ai_config_id": {"type": "integer", "description": "目标 AI 的 ai_config_id。"},
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
                        "常规 AI 协作请保持 false，对方的答复会作为新的 message.send_to_ai 调用回来。"
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
            "required": ["to_ai_config_id", "content", "message_type"],
        },
        handler=_ai_send_message,
        destructive=True,
    ))

    registry.register(MCPTool(
        name="prompt.manage",
        description=(
            "Prompt 统一工具：用 action 选择 list_targets 列目标 / read_ai 读 AI 人格 prompt / "
            "write_ai 改 AI 人格 prompt（需管理者+）/ read_system 读系统 prompt（需管理者+）/ "
            "write_system 改系统 prompt（需辅助管理员+）。prompt 正文存放在 KnowledgeBase 的 md 文件里。"
        ),
        input_schema=PROMPT_MANAGE_SCHEMA,
        handler=_prompt_manage,
        destructive=True,
    ))

    registry.register(MCPTool(
        name="device_mcp.manage",
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
