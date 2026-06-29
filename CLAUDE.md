# CLAUDE.md — server/ 后端 (HeySure-Server)

> 详细中文说明见 [`README.md`](README.md)。本文件是给 Claude 的快速导航与约定补充。

**本目录是独立仓库** `HeySure-Server`。多仓库工作区模式下通过根目录的 `init-env.ps1` / `init-env.sh` 拉取。

## 目录布局

```
server/
  main/                    ← 运行时核心（4 个进程 + 1 个共享层）
    api/                   ← 共享库（模型 / DB / 认证 / 服务 / 配置）
      core/                ← settings.py（配置总入口）/ logging / migrations
      models/              ← 21 个 SQLModel/ORM 数据模型
      services/            ← 业务逻辑，按域分 7 个子包（见下）
        knowledge/         ← 知识库/图书馆（kb_store / librarian_* / knowledge_*）
        tasks/             ← 任务系统（task_system/schedule/plan/completion_notify）
        mcp/               ← MCP 工具服务（tool_runner/prompt_groups/stats/tool_aliases）
        device_tools/      ← 设备工具（dynamic/workspace/permission + runtime 工具体）
        chat/              ← 聊天（persistence/media/compress）
        access/            ← 访问与治理（access_guards/auth_settings/governance）
        storage/           ← 二进制存储（screenshot_store/temp_image_store）
        （根部留 email_service / model_presets / repo_update / world_events 等孤立单例）
      devices/             ← 设备 helper（bindings/live/mcp_permissions/presence/workshop_bindings）
      chat_runtime/        ← 聊天编排（调度/流式/prompt组装/MCP解析）
      runtime/             ← 进程控制、心跳、内部 HTTP（internal_http + http_client）
      integrations/        ← 外部数据源（clawhub / media_source）
      common/              ← 跨进程小工具（value_utils：to_bool / safe_json*）
      database.py          ← DB 引擎（SQLModel + asyncpg）
      db.py                ← Alembic CLI（python -m api.db migrate/upgrade/...）
      sio.py               ← Socket.IO server 实例
      socket_events.py     ← socket 事件注册
      auth.py              ← JWT 认证中间件
    gateway/               ← 进程① 对外网关 (3000)
      main.py              ← 进程入口（uvicorn）
      app.py               ← FastAPI + Socket.IO 应用工厂（lifespan 在此）
      routers/             ← 29 个路由文件，按域拆分
    mcp_runtime/           ← 进程② MCP 工具运行时 (3001)
      main.py / app.py
      mcp/
        registry.py        ← 工具注册与查询
        permissions.py     ← 设备工具权限校验
        core.py            ← MCP 执行核心
        loader.py          ← 工具加载器
    connector_runtime/     ← 进程③ 连接器 (3002)
      main.py / app.py
      bots/                ← QQ / 飞书机器人（各含 adapter/router/service）
      dispatch/            ← 端侧消息分发
    ai_runtime/            ← 进程④ AI worker (3003)
      main.py
      inference/           ← 推理核心 / 消息服务 / 阶段上下文
      worker.py            ← 队列消费主循环
  library/                 ← 知识工坊内置虚拟 Agent（engine/handlers/policy/tools）
  tools/                   ← 工具箱内置设备（server 固定 MCP 工具集，13 个）
  other/
    migrations/            ← Alembic 迁移版本（22 个）
    scripts/               ← 运维脚本
    tests/                 ← pytest 单元测试（21 个）
  data/ logs/ venv/        ← 运行时产物（gitignore）
```

`PYTHONPATH` 需包含 `server/main` 与 `server` 根目录（`run_*.bat` 与 Dockerfile 已配置）。

## 心智模型：1 个共享层 + 4 个进程

每个 `*_runtime/main.py` 只做一件事：设置 `HEYSURE_SERVICE_ROLE` → import 共享 `api` → uvicorn 起端口。  
**改 `main/api/` 会同时影响 4 个进程。**

每个运行时目录下有两个文件：`app.py`（应用/路由工厂）和 `main.py`（进程入口，调 uvicorn）。

## api/ 内部分层

| 子目录 | 职责 |
| --- | --- |
| `core/settings.py` | **配置总入口**，45+ 个环境变量的真实清单 |
| `core/config.py` | 历史别名转发层（`DATABASE_URL` 等旧名称），新代码用 `settings` |
| `models/` | SQLModel/ORM 数据模型（21 个，见下表） |
| `services/` | 业务逻辑，按域分子包：`knowledge/` `tasks/` `mcp/` `device_tools/` `chat/` `access/` `storage/`（见下表），孤立单例留根部 |
| `devices/` | 设备相关 helper：`bindings`/`live`/`mcp_permissions`/`presence`/`workshop_bindings` |
| `chat_runtime/` | 聊天编排：调度、流式、prompt 组装、MCP 工具调用解析 |
| `runtime/` | 进程控制、心跳、内部 HTTP（`internal_http` 调其它 runtime；`http_client` 出站 AI 请求 `ai_http_post`） |
| `common/` | 跨进程小工具（`value_utils`：`to_bool` / `safe_json*`） |
| `database.py` | DB 引擎、连接池、session 工厂 |
| `db.py` | Alembic CLI（`python -m api.db migrate/current/upgrade/...`） |
| `sio.py` + `socket_events.py` | Socket.IO server 实例与事件注册 |
| `auth.py` | JWT Bearer 认证依赖项 |

## 关键数据模型速查

| 模型 | 文件 | 说明 |
| --- | --- | --- |
| `User` | `models/user.py` | 用户账号 |
| `Chat` / `ChatRun` | `models/chat.py` | 会话与单次推理执行记录 |
| `Agent` / `AgentConfig` | `models/ai_config.py` | AI 成员定义与配置 |
| `AiRuntimeState` | `models/ai_runtime.py` | AI 推理运行时状态 |
| `Task` / `TaskSchedule` | `models/task*.py` | 任务与定时调度规则 |
| `Device` / `DeviceBinding` | `models/device_binding.py` | 端侧设备注册与绑定 |
| `DevicePresence` | `models/device_presence.py` | 端侧设备在线状态快照 |
| `DeviceDynamicTool` | `models/device_dynamic_tool.py` | 设备上报工具定义 |
| `DevicePermissionPolicy` | `models/device_permission_policy.py` | 设备 MCP 工具权限策略 |
| `Memory` / `KnowledgeEntry` | `models/knowledge.py` | 记忆与知识条目（检索为纯文件关键词，无向量索引） |
| `McpTool` / `McpPermission` | `models/mcp*.py` | 工具定义与用户级权限 |
| `McpCallStat` | `models/mcp_call_stat.py` | MCP 调用统计 |
| `BotSessionRoute` | `models/bot_session_route.py` | 机器人会话路由 |
| `AdminAuditLog` | `models/admin_audit.py` | 管理员操作审计日志 |
| `WorldActorMeta` | `models/world_meta.py` | 世界观角色元数据 |
| `SystemConfig` | `models/system.py` | 系统级配置项 |

## 关键服务速查（按子包）

| 服务文件 | 职责 |
| --- | --- |
| **`tasks/`** | |
| `tasks/task_system.py` | 任务队列消费与调度器主循环 |
| `tasks/task_schedule.py` | 定时规则解析/校验/续期（**REST/MCP/调度器唯一权威**） |
| `tasks/task_plan.py` | 任务计划阶段管理 |
| `tasks/task_completion_notify.py` | 任务完成通知推送 |
| **`chat/`** | |
| `chat/chat_persistence.py` | 聊天消息与 ChatRun 持久化 |
| `chat/chat_media.py` | 聊天媒体文件处理 |
| `chat/conversation_compress.py` | 对话上下文压缩 |
| **`knowledge/`** | |
| `knowledge/kb_store.py` | 知识库文件存储 + 纯关键词检索（`keyword_search_knowledge`，无向量依赖） |
| `knowledge/knowledge_review_trigger.py` | 知识审核触发器 |
| `knowledge/librarian_service.py` | 知识工坊公共接口（propose/archive/consult/list_topics/brief/read） |
| `knowledge/librarian_core.py` | 图书馆共享底座（路径/会话/工具） |
| `knowledge/librarian_thoughts.py` | 传承思想 CRUD + NPX/全局技能 |
| `knowledge/librarian_builtins.py` | 内置条目（固有属性/人格/系统提示词） |
| `knowledge/librarian_clawhub.py` | ClawHub 集成（搜索/安装/更新） |
| `knowledge/library_mcp_catalog.py` | 图书馆 MCP 工具目录 |
| **`mcp/`** | |
| `mcp/mcp_tool_runner.py` | 通过 LLM 测试 MCP 工具（传承工具测试） |
| `mcp/mcp_prompt_groups.py` | MCP 提示词分组 |
| `mcp/mcp_stats.py` | MCP 调用统计 |
| `mcp/mcp_tool_aliases.py` | 工具名旧别名/重命名映射（迁移与运行时共用） |
| **`device_tools/`** | |
| `device_tools/device_permission_policy.py` | 设备 MCP 工具权限管理 |
| `device_tools/device_dynamic_tools.py` | 设备动态工具注册与管理 |
| `device_tools/device_workspace_tools.py` | 设备工作区工具文件管理 |
| `device_tools/device_runtime_tools/` | 出厂默认桌面工具体（bodies/*.py） |
| `device_tools/device_browser_runtime_tools/` | 出厂默认浏览器工具体 |
| **`access/`** | |
| `access/governance.py` | AI 成员治理（状态/权限/生命周期） |
| `access/access_guards.py` | 用户越权拦截 |
| `access/auth_settings.py` | 认证相关设置 |
| **`storage/`** | |
| `storage/screenshot_store.py` | 截图存储 |
| `storage/temp_image_store.py` | 临时图片存储 |
| **根部单例** | |
| `model_presets.py` | 模型预设管理 |
| `repo_update.py` | Git 仓库检测并拉取更新 |
| `email_service.py` | 邮件发送服务 |
| `world_events.py` | 世界观事件广播 |

## 路由文件速查（gateway/routers/）

| 文件 | 主要端点前缀 | 关键功能 |
| --- | --- | --- |
| `auth.py` | `/auth` | 登录 / 注册 / 刷新 token |
| `chat.py` + `chat_*_routes.py` | `/chat` | 创建会话 / 发消息 / 历史 / 流式 |
| `ai.py` + `ai_*_routes.py` | `/ai` | AI 成员 CRUD / 配置 / 任务 |
| `mcp.py` | `/mcp` | 工具列表 / 调用 / 权限设置 |
| `devices.py` + `device_*.py` | `/devices` | 设备注册 / 状态 / 工具下发 |
| `projects.py` | `/projects` | 项目 CRUD |
| `workshop.py` | `/workshop` | 知识工坊（创建/搜索） |
| `librarian_routes.py` | `/librarian` | 知识提议 / 审核 / ClawHub / 固有属性 |
| `admin.py` | `/admin` | 系统配置 / 审计日志 |
| `diagnostics.py` | `/diagnostics` | 健康检查 / 统计 |
| `bots.py` | `/bots` | QQ / 飞书机器人配置 |
| `execute.py` | `/execute` | 执行操作 |
| `temp_images.py` | `/tmp-images` | 临时图片上传与访问 |
| `repo_update.py` | `/repo` | Git 仓库检测并直接拉取更新 |
| `world.py` | `/world` | 世界观数据（角色 / 知识快照） |
| `socket_relay.py` | `/socket` | Socket.IO 中继 |

## "改 X 去哪里"

| 需求 | 位置 |
| --- | --- |
| 新增 REST 接口 | `main/gateway/routers/<域>.py`，文件名即域 |
| 新增 / 改数据模型 | `main/api/models/`，同时加 Alembic 迁移 `other/migrations/` |
| 业务逻辑 | `main/api/services/` |
| 设备在线状态/绑定/权限 | `main/api/devices/`（直接 `from api.devices.* import`） |
| 新增 MCP 工具（服务端固定） | `server/tools/`（handler）→ `mcp_runtime/mcp/registry.py`（注册） |
| 聊天推理编排 | `main/api/chat_runtime/orchestrator.py` |
| 聊天推理 worker | `main/ai_runtime/worker.py` + `inference/` |
| 定时/循环任务规则 | `main/api/services/tasks/task_schedule.py` |
| 知识工坊 Agent | `library/`，绑定接口在 `gateway/routers/workshop.py` |
| 知识库传承思想 CRUD | `main/api/services/knowledge/librarian_thoughts.py` |
| ClawHub 技能安装 | `main/api/services/knowledge/librarian_clawhub.py` |
| 固有属性/人格/系统提示词 | `main/api/services/knowledge/librarian_builtins.py` |
| 知识库公共接口（propose/consult/read） | `main/api/services/knowledge/librarian_service.py` |
| 机器人/连接器 | `connector_runtime/bots/`、`connector_runtime/dispatch/` |
| 配置项 | `main/api/core/settings.py` |

## 错误排查路径

| 症状 | 检查位置 | 典型原因 |
| --- | --- | --- |
| 进程启动失败 | 进程自身日志 `logs/` | 环境变量缺失 / 端口已占用 |
| DB 连接失败 | `api/database.py` | `DATABASE_URL` 格式错误或 PostgreSQL 未启动 |
| `/internal/*` 401 | `api/auth.py` 的 bearer 校验 | `HEYSURE_INTERNAL_TOKEN` 不一致 |
| 路由 404 | `gateway/routers/` 对应文件是否有该路径 | 路由未注册到 `gateway/app.py` 的 `app.include_router()` |
| 推理不响应 | `ai_runtime/worker.py` 日志；检查 3003 | 队列阻塞 / litellm 配置错误 / 模型 API key 缺失 |
| 工具调用失败 | `mcp_runtime/mcp/core.py` 日志；检查 3001 | 工具未注册 / 权限未开放 / 工具 handler 抛异常 |
| Socket.IO 端侧断连 | `connector_runtime/app.py` + `api/sio.py` | Connector (3002) 未启动 / 网络问题 |
| 任务不执行 | `services/tasks/task_system.py` 调度循环 | Gateway lifespan 未完成（调度器未启动） |
| 知识搜索为空 | `services/knowledge/kb_store.py` | 关键词未命中 / 文件未落在 topics/ 或技能目录 |
| 设备工具权限错误 | `mcp_runtime/mcp/permissions.py` | `DevicePermissionPolicy` 未配置该工具 |
| 设备在线状态异常 | `api/devices/presence.py` | `DevicePresence` 表记录异常 |
| MCP 工具测试失败 | `services/mcp/mcp_tool_runner.py` | 模型不支持 function calling / 设备离线 |

## 开发命令

```bash
cd server
pip install -r requirements.txt
set PYTHONPATH=main;.          # Windows；Linux/macOS 用 main:.

python -m gateway.main         # 3000，先起这个
python -m mcp_runtime.main     # 3001
python -m connector_runtime.main # 3002
python -m ai_runtime.main      # 3003

# 数据库迁移（Alembic 管理，唯一权威）
python -m api.db migrate       # 推荐：自动检测并升级
alembic upgrade head           # 等效
python -m api.db current       # 查看当前版本

# 测试
pytest                         # 读取 pytest.ini，测试在 other/tests/
```

## 注意点

- **进程间 `/internal/*` 需 `HEYSURE_INTERNAL_TOKEN` bearer**，四进程必须用同一个值。
- **数据库仅支持 PostgreSQL**，`DATABASE_URL` 为必填，psycopg3 驱动（`postgresql+psycopg://`）。
- **Gateway lifespan 有副作用**：启动时加载 MCP 插件、重置 agent presence、启动调度器；重启 Gateway 会触发这些操作。
- **`data/` `logs/` `venv/`** 为运行时产物，已 gitignore，不要提交。
- **改 `api/` 会影响全部 4 个进程**，注意某些逻辑只对特定 `HEYSURE_SERVICE_ROLE` 有意义。
- **设备 helper 一律 `from api.devices.* import`**（`bindings`/`live`/`mcp_permissions`/`presence`/`workshop_bindings`）；旧的 `api/device_*.py` 兼容 shim 已删除。
- **`core/config.py` 是旧别名层**（`DATABASE_URL` 等），新代码从 `api.core.settings` 导入 `settings`。
