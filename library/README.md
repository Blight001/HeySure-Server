# 内置知识工坊 Agent

`server/library` 提供每个账号自动上线的虚拟知识工坊 Agent。

当前状态：

- 保留 `EndpointAgentPresence` 在线快照与作坊/世界展示。
- 保留 `WorkshopAiBinding` 1:1 专用绑定。
- 保留 `/api/workshop/bindings` 管理接口。
- `tools.py` 注册传承思想列表、带行号详情、按行编辑、删除，以及通过 `npx skills` 安装并导入的 MCP。
- `tools.py` 另注册对知识库内置类目的读取/修改 MCP：传承技能（只读，展示工坊在线 MCP）、
  固有技能（读取 / 修改 MCP 工具描述与参数）、固有人格（读取 / 修改 AI 人格 Prompt）、
  固有思路（读取 / 修改所有 AI 统一使用的系统提示词）。这些写入复用 `librarian_service` 的保存逻辑，文件为真相源。
- `engine.execute_tool` 继续执行工具白名单、AI 归属、工坊绑定与角色权限复核。

后续添加工具时，在 `tools.py` 声明 schema，并在 `engine.py` 添加明确的
handler 映射。绑定、权限范围和前端展示链路可直接复用。
