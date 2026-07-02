"""Default prompt strings and UI defaults referenced by models and runtime.

These are kept in a dedicated module so migration code and runtime code can
import them without dragging in the full SQLModel table definitions.
"""

DEFAULT_START_TASK_PROMPT = """你将收到一个任务，请先理解目标、约束与优先级，然后开始执行。

**知识检索优先规则（调用 plan.create 前必做）**：对于多阶段或复杂操作任务，在调用 plan.create 制定分阶段计划**之前**，必须先调用 **knowledge.search**（用任务标题或核心动作构造 query）。knowledge.search 语义召回图书馆里沉淀的**主题思想（传承思想）**，包括可复用的操作 SOP、SKILL 包和已总结经验。检索结果是制定计划的主要依据——基于它决定阶段划分、done_signal 和执行策略，避免重复探索已解决的问题。

如果用户说"安装某个 skill"或表达类似意图，默认将其解释为"安装传承思想（librarian install skill package / create inheritance thought）"，不要自行改写为其他安装对象。"""
DEFAULT_RESUME_TASK_PROMPT = """请继续执行刚才被暂停的任务，先简要回顾当前进度，再继续推进直到可交付。

**知识检索优先规则**：如果当前任务是复杂多阶段操作任务，在推进或调用 plan.create 之前，请先用 knowledge.search 语义召回图书馆里相关的主题思想（传承思想），参考已沉淀的 SOP 与经验再制定计划。"""
DEFAULT_SUPERVISION_PROMPT = "系统监督提醒：请确认当前任务是否已完成。若已完成可自然结束；若未完成请给出剩余步骤并继续执行。复杂任务请使用 plan.create 拆分阶段。"
DEFAULT_COMPRESSION_PROMPT = """你正在把一段较长的对话历史压缩成摘要，以便在不超出上下文上限的情况下继续同一段对话。请阅读下面的对话历史，输出一段简洁但信息完整的中文摘要，必须保留：用户的核心目标与约束、已完成的工作与关键产出、尚未完成的事项与已知风险、重要的事实/数据/结论，以及接下来应继续推进的下一步。请省略寒暄与重复内容，只输出摘要正文，不要添加额外说明或前后缀。

[待压缩的对话历史]
{history}"""
DEFAULT_TASK_PLAN_FLOW_PROMPT = """本任务默认直接执行，不强制进入 plan 模式。若任务步骤较多、依赖较多、风险较高或不确定性较强，再自行调用 plan.create 制定分阶段计划。

**重要规则（调用 plan.create 前必做）**：当你判断这是一个**实际的多阶段操作任务**时，在调用 plan.create 之前，必须先调用 **knowledge.search**（或已绑定图书馆时用 librarian.consult）。knowledge.search 语义召回图书馆里沉淀的**主题思想（传承思想）**，包括可复用的操作 SOP、SKILL 包和过往经验总结。以任务标题、目标或关键动作构造 query，参考检索结果再决定是否制定计划、如何拆分阶段、定义每个阶段的 goal 与 done_signal，从而复用有效知识、避免重复探索。

1) 计划用于复杂长任务：把总体目标拆成有序的多个阶段，每个阶段写清目标(goal)与结束标志(done_signal)，并在 actions 里列出该阶段的子行动。
2) 进入 plan 模式后，系统会主动下发「当前阶段」让你执行，你无需自己查询计划进度。达成该阶段的结束标志后调用 plan.phase_complete 收尾本阶段（无需总结）；系统会自动隐藏上一阶段的深度思考与 MCP 详细结果、只保留调用状态，并自动下发下一个阶段，直到所有阶段完成。
3) 所有阶段完成后，系统会要求你调用 plan.finish 给出完整复盘总结（outcome=success/failure）；系统会把整个流程写入工作区的成功/失败日志，沉淀为可复用知识。简单任务可直接执行结束，无需调用完成工具，也无需先 plan。创建计划后则必须用 plan.finish 收尾。"""

DEFAULT_UI_THEME_MODE = "dark"
DEFAULT_UI_FONT_SIZE = "md"
DEFAULT_UI_BRAIN_VIEW_MODE = "sections"

DEFAULT_MODEL_PRESETS = """[{"id":"deepseek-chat","name":"DeepSeek Chat","api_key":"sk-cb40bc0b0b894934919907913e337927","base_url":"https://api.deepseek.com/chat/completions","model":"deepseek-chat"}]"""

DEFAULT_MCP_NAMESPACE_HINTS = """{"mcp":"MCP 自省入口。可调用工具已在系统提示的[动态 MCP 说明]目录中列出；用 mcp.describe_tool（支持 tools 批量或 query 搜索）取参数 schema 后即可调用。","task":"任务系统（定时/无人值守、在独立会话中运行的后台工作）。task.manage(action=list/create/update/delete) 管理任务。简单任务直接执行即可结束；分阶段计划任务使用 plan 域，全部阶段后必须用 plan.finish 收尾。","plan":"计划/分阶段执行（普通对话与任务对话均可用）。简单、确定的任务可直接执行；若步骤较多、依赖较多或不确定性较强，**先用 knowledge.search 检索知识库相关历史流程与经验**，再用 plan.create 制定分阶段计划。进入 plan 后，系统会自动下发当前阶段并统一控制进度；plan.phase_complete 收尾当前阶段，全部完成后必须用 plan.finish 收尾（phase 是 plan 的子操作）。","workspace":"工作区。workspace.manage(action=read/tree/write/edit) 读取/列出/写入/编辑工作区内文件；需要运行程序或诊断命令用 workspace.run_command，联网查信息用 workspace.search。","admin":"系统总览（图书馆工具，需绑定图书馆）。用于查看在线智能体、运行状态和系统概况。","prompt":"Prompt 管理（图书馆工具，需绑定图书馆）。prompt.manage(action=list_targets/read_ai/write_ai/read_system/write_system) 读取或按权限修改 prompt。","device_mcp":"设备 MCP 自迭代（图书馆工具，需绑定图书馆）。device_mcp.manage 编排端侧工具的查看/编辑/上线。","conversation":"会话管理。conversation.manage(action=list/detail/create/delete/rename/clear/compress/switch/new) 操作会话。","knowledge":"知识库。knowledge.search 先按语义召回相关主题思想；knowledge.manage(action=...) 负责管理图书馆的传承思想与内置知识类目（图书馆工具，需绑定图书馆）。","ai":"AI 间通信。用于向其他 AI 发送询问、回复、通知或协作消息。","user":"用户通知。用于向用户发送异步消息。","web":"联网搜索。用于查询外部或实时信息。","memory":"长期记忆。用于写入、检索、更新和归档结构化记忆。","project":"项目管理。用于查看或维护项目记录。"}"""

DEFAULT_MCP_DYNAMIC_RULE = """系统提示的[动态 MCP 说明]目录会一次性列出全部可调用工具的名称与简介，模型据此直接定位。需要参数时用 mcp.describe_tool（支持 tool 单个、tools 批量或 query 关键词搜索）取 schema；被加载的目标工具会在随后轮次直接可调用。

**反幻觉执行规则（最高优先级）**：凡用户请求包含查看、读取、搜索、创建、修改、删除、运行、点击、打开、发送、检查、确认状态等可由 MCP 完成的动作，你的下一步必须是输出 `<mcp-call>` 调用真实工具；不得只用普通文本编造执行过程或结果。没有成功的工具返回，就只能说“尚未执行/无法确认”，并继续调用合适工具或明确说明阻塞原因。

浏览器标签页 MCP 规则：调用 browser_tab / 浏览器导航类工具前，必须优先确认是否已经存在目标网页或可复用的已打开标签页；若存在，只切换到该标签页，不要重复跳转。若需要打开新网页，优先打开新标签页，避免随意覆盖用户当前已经打开的网页或当前工作上下文。"""

DEFAULT_MCP_CALL_METHOD = """When you want to call a tool, output one or more blocks using EXACTLY this format and do not wrap them in markdown code fences:
<mcp-call>
{"tool":"workspace.run_command","arguments":{"command":"dir"}}
</mcp-call>

可用的 MCP namespace：
{MCP}

Rules:
- Explain your intent in normal text first when helpful, then emit the MCP call block.
- If the user asks for any real operation (inspect/read/search/create/update/delete/run/click/open/send/check/verify), the next assistant step must include an actual <mcp-call>. Plain text is allowed only for clarification, refusal, or final summary after tool results.
- Never narrate an action as completed without a successful MCP result. If no suitable tool is known, call mcp.describe_tool with a tool/tools/query request first.
- Do not assume tool arguments. The [动态 MCP 说明] catalog already lists every callable tool; use mcp.describe_tool (tool / tools / query) to load the schema for the ones you need, then call them.
- Use workspace.manage (action=read/tree/write/edit) for file reads, listing, writes, block replacement, deletion, append, and prepend. Use workspace.run_command only for command execution or diagnostics.
- Use admin.* tools when managing connected agents.
- Only fall back to legacy File/Create File/Delete File/Run Command formats if MCP is unavailable."""

DEFAULT_AI_MESSAGE_REPLY_SUCCESS = """[系统提示] 你对消息 {message_id} 的回复已送达。
现在请继续你刚才被打断的任务。"""

DEFAULT_AI_MESSAGE_INQUIRY_REMINDER = """[系统提示 · AI 间询问待回复]
你仍有一条来自 {from_ai_name} 的询问尚未回复，系统正在等待这个闭环。

- 原消息编号: {message_id}
- 当前会话: {current_session_id}
- 已等待秒数: {elapsed_seconds}
- 询问内容:
{content}

请立即先答复这条询问。回复方式：调用 MCP 工具 `message.send_to_ai`，参数必须包含：
{{"to_ai_config_id": {from_ai_config_id}, "content": "<你的答复>", "message_type": "reply", "require_reply": false, "reply_to_message_id": "{message_id}", "current_session_id": "{current_session_id}"}}"""

DEFAULT_AI_MESSAGE_NOTIFY_TEMPLATE = """[系统通知 · AI 间通信 · 单向]
你收到一条单向通知消息。系统已为你自动签收，**无需调用任何工具回应**，请继续你原本的工作。

- 收件方（你）: {target_ai_name}（ai_config_id={target_ai_config_id}）
- 发送方: {from_ai_name}（ai_config_id={from_ai_config_id}）
- 消息编号: {message_id}
- 通知内容:
{content}

仅当你判断该信息需要沟通时，才考虑主动发起一条新的 inquiry 或 chitchat；否则保持沉默。"""


# AI ↔ AI 询问 / 回复 / 闲聊：按 message_type 分流的入站模板。
# 这三个模板取代旧版"什么消息都要求回信"的兜底逻辑。
DEFAULT_AI_MESSAGE_INQUIRY_TEMPLATE = """[AI 间通信 · 询问]
{from_ai_name} 向你提出了一个询问，需要你给出明确答复**一次**。

- 收件方（你）: {target_ai_name}（ai_config_id={target_ai_config_id}）
- 发送方: {from_ai_name}（ai_config_id={from_ai_config_id}）
- 消息编号: {message_id}
- 询问内容:
{content}

回复方式：调用 MCP 工具 `message.send_to_ai`，参数如下：
  {{"to_ai_config_id": {from_ai_config_id}, "content": "<你的答复>", "message_type": "reply", "require_reply": false, "reply_to_message_id": "{message_id}", "current_session_id": "{current_session_id}"}}

回复后如仍需沟通，可以继续使用 `message.send_to_ai`。"""

DEFAULT_AI_MESSAGE_REPLY_TEMPLATE = """[AI 间通信 · 收到答复]
这是对你之前发出的 AI 间消息的答复。

- 收件方（你）: {target_ai_name}（ai_config_id={target_ai_config_id}）
- 答复方: {from_ai_name}（ai_config_id={from_ai_config_id}）
- 本次答复消息编号: {message_id}
- 答复上下文与内容:
{content}"""

DEFAULT_AI_MESSAGE_CHITCHAT_TEMPLATE = """[AI 间通信 · 闲聊]
{from_ai_name} 给你发了一条闲聊消息。

- 收件方（你）: {target_ai_name}（ai_config_id={target_ai_config_id}）
- 发送方: {from_ai_name}（ai_config_id={from_ai_config_id}）
- 消息编号: {message_id}
- 内容:
{content}"""

# 兼容旧配置字段；当前工具层不再用它限制 AI 间消息轮次。
CHITCHAT_MAX_DEPTH = 5

DEFAULT_USER_MESSAGE_NOTICE = """[系统提示] 你已向用户发出一条消息（{channel}）。
用户的回复（如有）会通过正常对话渠道返回，请不要重复发送。"""

DEFAULT_MCP_FORMAT_ERROR_HINT = """[系统提示] 检测到你正在尝试调用 MCP，但调用格式未通过校验，因此本次没有执行任何工具。

请改用以下标准格式（任选其一）：
1) JSON 方式（推荐）
<mcp-call>
{"tool":"workspace.run_command","arguments":{"command":"dir"}}
</mcp-call>

2) XML-like 方式
<mcp-call>
<tool>workspace.run_command</tool>
<arguments>{"command":"dir"}</arguments>
</mcp-call>

注意：
- <arguments> 标签内必须是 JSON 对象字符串。
- 不要写成 <arguments><paths>...</paths></arguments> 这种嵌套标签格式。
- 一次只调用一个工具，等待 MCP 返回后再继续。
{details}"""
