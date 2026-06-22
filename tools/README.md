# 内置工具箱设备（server/tools）

`server/tools` 提供服务端内置的「工具箱」虚拟设备，与图书馆（`server/library`）并列。

工具箱把"每个 AI 默认即可用的服务端固定 MCP 工具"收拢成一个独立设备。工具实现代码（原位于 mcp_runtime/mcp/tools/）也独立放在本目录下，与 engine 共同构成完整的工具箱作坊设备：

- **多绑、可管理**：新建 AI 自动绑定工具箱（`bind_config_to_toolbox`）。
  之后完全由用户在网页「作坊」面板或 AI 配置中绑定/解绑、管理 MCP 范围（存 DeviceTypeMcpPermission）。
  不再做全量自愈补绑。
- **不注册 presence、不经工坊分发**：工具箱只是一个绑定标记 + 展示条目
  （`toolbox_connected_entry_for_user`）；工具箱工具仍来自常规服务端注册表
  （`MCPRegistry`），由 `mcp_runtime` 在每次调用时按工具箱绑定逐项校验。
- **门禁判定**：`is_toolbox_gated_tool` / `enforce_toolbox_binding` 判断"哪些服务端
  固定工具属于工具箱、是否已绑定"，是注册表核心 `MCPRegistry.call` 的唯一来源——
  中央权限层不再内联工具箱特例。

## 与服务器的连接点（运行时）

| 位置 | 用途 |
| --- | --- |
| `mcp_runtime/mcp/core.py` | `MCPRegistry.call` 经 `enforce_toolbox_binding` 校验绑定（逻辑定义在 tools/engine） |
| `api/device_live.py` · `gateway/routers/devices.py` | 把工具箱合成为一条常在线"已连接设备" |
| `gateway/routers/workshop.py` | 工具箱绑定列表 / 绑定接口（多绑） |
| `gateway/routers/ai_config_routes.py` · `ai_runtime/.../ai_service.py` | 新建 AI 时默认绑定工具箱 |
| `library/engine.py` | 图书馆 presence 自愈时仅确保工具箱 MCP scope 默认记录 |
| `api/chat_runtime/chat_prompt_utils.py` | 按工具箱绑定过滤可见工具目录 |

后续扩展工具箱能力时，在本模块声明逻辑，调用方按上表注入即可，无需再回到中央
权限层或注册表核心打补丁。
