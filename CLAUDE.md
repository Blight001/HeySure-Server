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
      services/            ← 60+ 个业务逻辑文件
      chat_runtime/        ← 聊天编排（调度/流式/prompt组装/MCP解析）
      runtime/             ← 进程控制、心跳、内部 HTTP 客户端
      integrations/        ← 外部数据源（clawhub / media_source）
      database.py          ← DB 引擎（SQLModel + asyncpg）
      sio.py               ← Socket.IO server 实例
      socket_events.py     ← socket 事件注册
    gateway/               ← 进程① 对外网关 (3000)
      main.py              ← FastAPI app + lifespan（启动时加载MCP/重置presence/起调度器）
      routers/             ← 29 个路由文件，按域拆分
    mcp_runtime/           ← 进程② MCP 工具运行时 (3001)
      main.py
      mcp/
        registry.py        ← 工具注册与查询
        permissions.py     ← 设备工具权限校验
        core.py            ← MCP 执行核心
        loader.py          ← 工具加载器
    connector_runtime/     ← 进程③ 连接器 (3002)
      main.py
      bots/                ← QQ / 飞书机器人（各含 adapter/router/service）
      dispatch/            ← 端侧消息分发
    ai_runtime/            ← 进程④ AI worker (3003)
      main.py
      inference/           ← 推理核心 / 消息服务 / 阶段上下文
      worker.py            ← 队列消费主循环
  library/                 ← 知识工坊内置虚拟 Agent（engine/handlers/policy/tools）
  tools/                   ← 工具箱内置设备（server 固定 MCP 工具集）
  other/
    migrations/            ← Alembic 迁移版本
    scripts/               ← 运维脚本
    tests/                 ← pytest 单元测试
  data/ logs/ venv/        ← 运行时产物（gitignore）
```

`PYTHONPATH` 需包含 `server/main` 与 `server` 根目录（`run_*.bat` 与 Dockerfile 已配置）。

## 心智模型：1 个共享层 + 4 个进程

每个 `*_runtime/main.py` 只做一件事：设置 `HEYSURE_SERVICE_ROLE` → import 共享 `api` → uvicorn 起端口。
**改 `main/api/` 会同时影响 4 个进程。**

## api/ 内部分层

| 子目录 | 职责 |
| --- | --- |
| `core/settings.py` | **配置总入口**，45+ 个环境变量的真实清单 |
| `models/` | SQLModel/ORM 数据模型（user/chat/project/agent/knowledge/device/task/mcp/workshop…） |
| `services/` | 业务逻辑（60+ 文件，按功能域命名） |
| `chat_runtime/` | 聊天编排：调度、流式、prompt 组装、MCP 工具调用解析 |
| `runtime/` | 进程控制、心跳、内部 HTTP 客户端（调其它 runtime 用） |
| `database.py` | DB 引擎、连接池、session 工厂 |
| `sio.py` + `socket_events.py` | Socket.IO server 实例与事件注册 |

## 关键数据模型速查

| 模型 | 文件 | 说明 |
| --- | --- | --- |
| `User` | `models/user.py` | 用户账号 |
| `Chat` / `ChatRun` | `models/chat.py` | 会话与单次推理执行记录 |
| `Agent` / `AgentConfig` | `models/agent*.py` | AI 成员定义与配置 |
| `Task` / `TaskSchedule` | `models/task*.py` | 任务与定时调度规则 |
| `Device` / `DeviceBinding` | `models/device*.py` | 端侧设备注册与绑定 |
| `Knowledge` / `KnowledgeVector` | `models/knowledge*.py` | 知识条目与向量索引 |
| `DevicePermissionPolicy` | `models/device_permission_policy.py` | 设备 MCP 工具权限策略 |
| `McpTool` / `McpPermission` | `models/mcp*.py` | 工具定义与用户级权限 |

## 关键服务速查

| 服务文件 | 职责 |
| --- | --- |
| `task_system.py` | 任务队列消费与调度器主循环 |
| `task_schedule.py` | 定时规则解析/校验/续期（**REST/MCP/调度器唯一权威**） |
| `chat_persistence.py` | 聊天消息与 ChatRun 持久化 |
| `kb_store.py` | 知识库向量存储（pgvector，embedding via litellm） |
| `librarian_service.py` | 知识工坊提议/审核/沉淀流程 |
| `governance.py` | AI 成员治理（状态/权限/生命周期） |
| `device_permission_policy.py` | 设备 MCP 工具权限管理 |
| `access_guards.py` | 用户越权拦截 |

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
| `librarian_routes.py` | `/librarian` | 知识提议 / 审核 |
| `admin.py` | `/admin` | 系统配置 / 审计日志 |
| `diagnostics.py` | `/diagnostics` | 健康检查 / 统计 |
| `bots.py` | `/bots` | QQ / 飞书机器人配置 |
| `execute.py` | `/execute` | 执行操作 |
| `temp_images.py` | `/tmp-images` | 临时图片上传与访问 |
| `repo_update.py` | `/repo` | Git webhook 触发更新 |

## "改 X 去哪里"

| 需求 | 位置 |
| --- | --- |
| 新增 REST 接口 | `main/gateway/routers/<域>.py`，文件名即域 |
| 新增 / 改数据模型 | `main/api/models/`，同时加 Alembic 迁移 `other/migrations/` |
| 业务逻辑 | `main/api/services/` |
| 新增 MCP 工具（服务端固定） | `server/tools/`（handler）→ `mcp_runtime/mcp/registry.py`（注册） |
| 聊天推理编排 | `main/api/chat_runtime/orchestrator.py` |
| 聊天推理 worker | `main/ai_runtime/worker.py` + `inference/` |
| 定时/循环任务规则 | `main/api/services/task_schedule.py` |
| 知识工坊 Agent | `library/`，绑定接口在 `gateway/routers/workshop.py` |
| 机器人/连接器 | `connector_runtime/bots/`、`connector_runtime/dispatch/` |
| 配置项 | `main/api/core/settings.py` |

## 错误排查路径

| 症状 | 检查位置 | 典型原因 |
| --- | --- | --- |
| 进程启动失败 | 进程自身日志 `logs/` | 环境变量缺失 / 端口已占用 |
| DB 连接失败 | `api/database.py` | `DATABASE_URL` 格式错误或 PostgreSQL 未启动 |
| `/internal/*` 401 | `api/auth.py` 的 bearer 校验 | `HEYSURE_INTERNAL_TOKEN` 不一致 |
| 路由 404 | `gateway/routers/` 对应文件是否有该路径 | 路由未注册到 `gateway/main.py` 的 `app.include_router()` |
| 推理不响应 | `ai_runtime/worker.py` 日志；检查 3003 | 队列阻塞 / litellm 配置错误 / 模型 API key 缺失 |
| 工具调用失败 | `mcp_runtime/mcp/core.py` 日志；检查 3001 | 工具未注册 / 权限未开放 / 工具 handler 抛异常 |
| Socket.IO 端侧断连 | `connector_runtime/app.py` + `api/sio.py` | Connector (3002) 未启动 / 网络问题 |
| 任务不执行 | `services/task_system.py` 调度循环 | Gateway lifespan 未完成（调度器未启动） |
| 知识搜索为空 | `services/kb_store.py` | 向量未写入 / embedding 维度不匹配 |
| 设备工具权限错误 | `mcp_runtime/mcp/permissions.py` | `DevicePermissionPolicy` 未配置该工具 |

## 开发命令

```bash
cd server
pip install -r requirements.txt
set PYTHONPATH=main;.          # Windows；Linux/macOS 用 main:.

python -m gateway.main         # 3000，先起这个
python -m mcp_runtime.main     # 3001
python -m connector_runtime.main # 3002
python -m ai_runtime.main      # 3003

# 数据库迁移
alembic upgrade head

# 测试
pytest                         # 读取 pytest.ini，测试在 other/tests/
```

## 注意点

- **进程间 `/internal/*` 需 `HEYSURE_INTERNAL_TOKEN` bearer**，四进程必须用同一个值。
- **数据库仅支持 PostgreSQL**，`DATABASE_URL` 为必填，psycopg3 驱动（`postgresql+psycopg://`）。
- **Gateway lifespan 有副作用**：启动时加载 MCP 插件、重置 agent presence、启动调度器；重启 Gateway 会触发这些操作。
- **`data/` `logs/` `venv/`** 为运行时产物，已 gitignore，不要提交。
- **改 `api/` 会影响全部 4 个进程**，注意某些逻辑只对特定 `HEYSURE_SERVICE_ROLE` 有意义。
