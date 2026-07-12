# HeySure 服务接入协议 —— AI 实施指南（read.md）

> **本文档的读者是 AI 编码代理。** 你的任务：按本文档改造当前项目，让它成为
> HeySure 平台上 AI 成员可调用的服务。交付两个模块：
>
> 1. **登录连接模块**：REST 登录换 token → Socket.IO 长连接 → 注册；断线、token
>    失效均自动恢复。
> 2. **MCP 转换层**：把本项目已有能力（函数 / 内部 API / DB 查询）封装成 MCP
>    工具，接收调用、路由执行、回传结果。
>
> 命名约定：协议事件与字段沿用历史命名 `device`（`device:register`、`deviceId`），
> 指的就是你注册的这个服务实例；网页控制台把它显示为一台"自定义设备"。

---

## 0. 实施流程（按序执行）

| 步骤 | 做什么 | 依据 | 验收 |
| --- | --- | --- | --- |
| 1 | 盘点项目能力，设计工具清单 | 0.1 | 产出工具表：名称 / 描述 / 参数 schema / 对应的项目函数 |
| 2 | 确定集成形态 | 0.2 | 嵌入项目进程，或独立 adapter 进程 |
| 3 | 实现登录模块 | 0.3、3 | 拿到 `access_token` 与 `agent_socket_url` |
| 4 | 实现连接与注册 | 3.2、4 | 收到 `device:registered` |
| 5 | 实现 MCP 转换层 | 5、8 | 每个 `taskId` 恰好一次回包 |
| 6 | 验证并交接给用户 | 12 | `GET /api/devices/connected` 可见本服务；提示用户去控制台绑定 AI + 勾选权限 |

### 0.1 工具设计规则

- 从项目**已有**能力中挑选，不要为接入新造功能。起步 3~10 个工具。
- 每个工具一个明确动作，参数尽量少。优先暴露只读查询；写操作 / 不可逆操作
  必须在描述中写明并标 `destructive: true`。
- **禁止**设计"万能工具"（如 `run_sql`、`eval`），除非用户明确要求。
- 命名 `<域>.<动作>`，全小写，域取业务域：`order.query`、`report.generate`。
  保留前缀见 5.1，**不得占用**。

### 0.2 集成形态二选一

- **A 嵌入式**：项目本身是常驻进程且可改代码 → 把接入模块作为项目的一个组件启动。
- **B 边车式**：项目不常驻、不便改动、或语言不便跑 Socket.IO → 写一个独立
  adapter 进程，通过项目已有的 API / CLI / DB 调用其能力。
- 判断不了就选 B（对原项目零侵入）。

### 0.3 配置契约

凭据与地址一律走环境变量（或项目既有配置体系），**不得硬编码**：

```
HEYSURE_SERVER=http://<网关>:3000    # HeySure API Gateway 地址
HEYSURE_ACCOUNT=<账号>               # 与网页控制台同一账号，服务属于该用户
HEYSURE_PASSWORD=<密码>
HEYSURE_SERVICE_ID=<项目名>-01       # 逻辑 ID：稳定唯一，重启/重连后必须不变
HEYSURE_SERVICE_NAME=<展示名>        # 网页面板与 AI 看到的服务名
```

---

## 1. 协议总览

```
你的项目 ──① POST /api/auth/login──────────► API Gateway (:3000)
         ◄── access_token + agent_socket_url ─┘
         ──② Socket.IO 连接 + device:register ─►
         ◄── device:registered ───────────────┘
         ◄──③ task:dispatch {tool, args} ─────  （AI 发起调用）
         ──── task:result / task:error ───────►
```

硬性事实：

- 你的项目**不需要任何 AI 能力**。推理、编排、决定何时调用，全在 HeySure 服务端；
  项目只负责"被调用时执行、恰好回一次结果"。
- 工具目录完全由注册包自报（名称 + 描述 + JSON Schema），服务器原样转交给模型：
  **你上报什么，AI 就看到什么**。
- 自建服务固定声明 `deviceType: "custom"`，与官方端同级调度（presence、绑定、
  权限、任务队列全部通用）。
- 服务在线 ≠ AI 可调用。还需两道闸门（绑定 + 授权，第 7 节），由人在网页控制台
  操作，默认全关。

---

## 2. 实施模板（Python）

依赖：`pip install "python-socketio[client]" requests`（Socket.IO v4 协议）。
复制后只需替换 ①工具清单 与 ②工具实现，③④⑤ 原样保留即可。

```python
import os
import requests
import socketio

SERVER       = os.getenv("HEYSURE_SERVER", "http://127.0.0.1:3000")
ACCOUNT      = os.environ["HEYSURE_ACCOUNT"]
PASSWORD     = os.environ["HEYSURE_PASSWORD"]
SERVICE_ID   = os.getenv("HEYSURE_SERVICE_ID", "myproject-01")
SERVICE_NAME = os.getenv("HEYSURE_SERVICE_NAME", "我的项目")

# ── ① 工具清单（name/description/schema 全部对模型可见，见第 5 节） ──────────
TOOLS = [
    {
        "name": "order.query",
        "description": "按订单号查询订单状态与金额",
        "input_schema": {
            "type": "object",
            "properties": {"order_id": {"type": "string", "description": "订单号"}},
            "required": ["order_id"],
        },
    },
]

# ── ② 工具实现：工具名 → 项目内真实函数，返回 (result, summary) ──────────────
def handle_order_query(args):
    # TODO: 换成本项目已有的逻辑（调函数 / 内部 API / 查库）
    return {"order_id": args["order_id"], "status": "已发货"}, f"订单 {args['order_id']} 已发货"

HANDLERS = {"order.query": handle_order_query}

# ── ③ 登录模块：换 token；token 失效时重新调用 ──────────────────────────────
STATE = {"token": None, "socket_url": SERVER, "registered": False}

def login():
    r = requests.post(f"{SERVER}/api/auth/login",
                      json={"account": ACCOUNT, "password": PASSWORD}, timeout=10)
    r.raise_for_status()
    data = r.json()
    STATE["token"] = data["access_token"]
    STATE["socket_url"] = data.get("agent_socket_url") or SERVER  # 永远优先用该字段

# ── ④ 连接与注册 ────────────────────────────────────────────────────────────
sio = socketio.Client(reconnection=True, reconnection_delay=2)

def register():
    sio.emit("device:register", {
        "id": SERVICE_ID,
        "name": SERVICE_NAME,
        "platform": "custom-service",   # 自由字符串；勿含 desktop/browser/android/workshop
        "deviceType": "custom",         # 固定值
        "token": STATE["token"],
        "version": "1.0.0",
        "capabilities": [t["name"] for t in TOOLS],
        "toolDefs": TOOLS,
    })

@sio.event
def connect():                          # 每次(重)连接都要重新注册
    STATE["registered"] = False
    register()
    def retry():                        # 收到确认前每 3 秒重发，防握手期丢包
        while sio.connected and not STATE["registered"]:
            sio.sleep(3)
            if not STATE["registered"]:
                register()
    sio.start_background_task(retry)

@sio.on("device:registered")
def on_registered(data):
    STATE["registered"] = True          # data["aiConfigId"] 为 null 时提示用户去绑定

@sio.on("device:register_rejected")
def on_rejected(data):                  # 最常见原因：token 过期 → 重登录再注册
    login()
    register()

# ── ⑤ MCP 转换层：接收调用 → 路由 → 恰好一次回包 ────────────────────────────
@sio.on("task:dispatch")
def on_task(task):
    task_id, tool = task.get("taskId"), task.get("tool")
    try:
        handler = HANDLERS.get(tool)
        if handler is None:
            raise ValueError(f"unknown tool: {tool}")
        result, summary = handler(task.get("args") or {})
        sio.emit("task:result", {"taskId": task_id, "deviceId": SERVICE_ID,
                                 "success": True, "tool": tool,
                                 "result": result, "summary": summary})
    except Exception as exc:
        sio.emit("task:error", {"taskId": task_id, "deviceId": SERVICE_ID,
                                "error": str(exc)})

login()
sio.connect(STATE["socket_url"])
sio.wait()
```

---

## 3. 登录与连接（协议细节）

### 3.1 登录

```
POST {SERVER}/api/auth/login
Content-Type: application/json

{"account": "<账号>", "password": "<密码>"}
```

响应（节选）：

```json
{
  "access_token": "eyJhbGciOi...",
  "token_type": "bearer",
  "agent_socket_url": "http://your-server:3000",
  "user": {"id": 1, "account": "heysure"}
}
```

规则：

- `access_token` 放进 `device:register` 的 `token` 字段；也是所有辅助 REST
  接口（第 11 节）的 Bearer token。
- Socket.IO 连接地址**必须用 `agent_socket_url`**，不要写死端口（服务器可能
  经反代或 `AGENT_SOCKET_URL` 配置了不同的对外地址）。
- token 有效期由服务端决定。收到 `device:register_rejected` 且 reason 提示
  token 无效 → 重新登录 → 重新注册（模板 ③ 已实现）。
- 已持有 token 时可用 `GET /api/auth/agent-endpoint` 单独刷新 `agent_socket_url`。

### 3.2 Socket.IO 连接参数

| 参数 | 值 |
| --- | --- |
| URL | 登录响应的 `agent_socket_url` |
| path / 命名空间 | `/socket.io/` / 默认命名空间 `/`（都用默认值） |
| 协议版本 | Socket.IO v4（JS `socket.io-client` ≥ 4.x / `python-socketio` ≥ 5.x） |
| 传输 | polling → websocket 自动升级 |
| 单帧上限 | 20 MB（大图压缩或分片） |

---

## 4. 注册协议：`device:register`

连接（含每次重连）成功后立即发送；收到 `device:registered` 前每 3 秒重发。

```jsonc
{
  // 必填
  "id": "myproject-01",          // 逻辑 ID：稳定唯一。绑定/权限/任务队列按它落库
  "name": "我的项目",             // 展示名
  "token": "<access_token>",     // 用户 JWT（服务器校验后即时删除，不落库）
  "deviceType": "custom",        // 固定 "custom"；勿冒充内置类型
                                 // （desktop/browser/android/workshop）
  // 强烈建议
  "platform": "custom-service",  // 自由字符串；勿含 desktop/windows/browser/
                                 // android/workshop（旧版按关键词分类）
  "capabilities": ["order.query"],   // 工具名清单
  "toolDefs": [ /* 第 5 节 */ ],     // 工具自描述；缺失项会被兜底成无 schema 宽松模式
  "version": "1.0.0",
  // 可选
  "icon": "3",                   // "1"~"8" 预置编号 / "/device_png/N.webp" / 绝对 URL；
                                 // 不填走网页默认；改后重注册生效
  "lifecycle": "registered",
  "group": "",
  "os": {"platform": "linux", "arch": "x64", "hostname": "prod-01"}   // 仅展示
}
```

服务器回应（发给当前 socket）：

| 事件 | 载荷 | 含义 |
| --- | --- | --- |
| `device:registered` | `{"id", "aiConfigId"}` | 成功。`aiConfigId` 为已持久化的 AI 绑定，null = 未分配 |
| `device:register_rejected` | `{"reason"}` | 被拒。典型原因：token 缺失/过期、绑定的 AI 不属于该用户 |

注册成功后服务器：写入在线 presence 快照（工具目录以此做发现）→ 重放掉线期间
积压的任务 → 向属主网页推送最新列表。

约束：

- **服务不能自选 AI**，绑定只由人在网页作坊面板分配，注册时自动套用已持久化的绑定。
- **同一 AI 每种执行端类型最多绑一个**；要接多个自建服务就绑到不同 AI。
- 重连用同一个 `id` 重新注册即可，绑定与权限自动恢复。

---

## 5. MCP 工具自描述：`toolDefs`

每个元素描述一个 MCP 工具。**服务是自己工具 schema 的唯一权威**——服务器原样
存储并转交给模型。

```jsonc
{
  "name": "order.query",               // 必填，与 capabilities 中的名字一致
  "description": "按订单号查询订单状态与金额。",  // 必填，模型据此决定何时调用
  "input_schema": {                    // 必填，标准 JSON Schema（也接受 inputSchema）
    "type": "object",
    "properties": {
      "order_id": {"type": "string", "description": "订单号，如 SO-2026-0001"}
    },
    "required": ["order_id"]
  },
  "destructive": false                 // 可选：危险/不可逆操作标记（UI 提示用）
}
```

### 5.1 命名规范与保留字

- 格式 `<域>.<动作>`，全小写；同一账号下保持唯一（重名时服务器优先派发给真正
  申报了该工具的服务，但唯一命名最稳妥）。
- **保留前缀，禁止使用**（会被服务器归到别的通道或剥离）：
  - `browser_` / `card_` —— 浏览器扩展通道专用
  - `evolution.` / `librarian.` —— 知识与进化工坊专用
  - `remote_control` / `remote.control`、`remote_terminal` / `remote.terminal`
    —— 远程连接能力保留字（不是可调用工具，见第 9 节）
  - `rc:` / `rt:` —— 远程连接事件前缀
- 只进 `capabilities` 不写 `toolDefs` 的工具也能被调用，但模型只能盲传参数。
  **始终写全 toolDefs。**

### 5.2 描述写法

`description` 是模型决定"何时调用、怎么传参"的唯一依据：一句话说清做什么 +
关键参数含义 + 明显限制（"仅支持 PNG"、"耗时约 10s"）。参数逐个写
`properties.*.description`，必填项进 `required`。

---

## 6. 动态 MCP：服务器下发的工具（勿实现）

**第三方服务不要实现本通道**——第 5 节的静态 `toolDefs` 才是接入的基本要求。
本节仅为划清边界（官方桌面端 / 浏览器扩展才实现它）：

|  | 静态 `toolDefs`（第 5 节） | 动态 MCP（本节） |
| --- | --- | --- |
| 定义存在哪 | 你的服务代码里 | 服务器（按用户 + 执行端类型存储） |
| 谁能改 | 服务开发者（改代码重发布） | 已绑定"图书馆"的 AI（`device_mcp.manage`）或人在控制台；执行端自己不能改 |
| 生效时机 | 重新 `device:register` | 服务器经 `device:tool-config` 事件实时整体推送 |
| 执行端职责 | 执行自己写的逻辑 | 接收定义、合并进本地目录、**沙箱执行服务器下发的代码** |

`device:tool-config` 载荷（供官方端实现参考）：

```jsonc
{
  "tools": [{
    "name": "fs.read_better",
    "description": "...",
    "input_schema": { "type": "object", "properties": {} },
    "code_kind": "js",              // "program" | "js" | "runtime"
    "js": "return await cap.call('fs.read', args)"
    // program → "code": [{op:'call'|'set'|'return', ...}]
    // runtime → "runtime": "powershell"|"shell"|"python" + "source": "..."
    //           （Windows 仅 powershell/shell；Linux/macOS 三者都支持）
  }],
  "permissionPolicy": { "...": "..." }   // 可选：runtime 工具权限策略
}
```

推送时机：每次注册成功后补发完整集合；之后每次编辑实时推送。官方端做法
（`executor/dynamic.ts`）：校验 → 合并路由表 → 重新 `device:register` 上报合并后
清单 → 断线时清空。调用协议与静态工具完全相同（第 8 节），服务器只按工具名派发。

如果用户 / AI 在控制台创建了动态工具而你的服务"调不到"：这是预期行为，
你没实现（也不需要实现）该通道。

---

## 7. 绑定与授权：两道闸门

服务在线 ≠ AI 可调用。两道闸门默认全关：

```
服务在线（presence）
   └─► 闸门 1：服务 ↔ AI 绑定       POST /api/devices/bind
          └─► 闸门 2：工具授权范围   PUT /api/devices/{id}/mcp-scope
                 └─► 工具出现在 AI 的系统提示词中，可被调用
```

- **闸门 1**：`POST /api/devices/bind`，body `{"deviceId": "myproject-01",
  "aiConfigId": 3}`（null 解绑）。按 `(用户, 服务id)` 持久化，重连自动恢复，
  可在服务离线时预先绑定。
- **闸门 2**：`PUT /api/devices/{deviceId}/mcp-scope`，body
  `{"tools": ["order.query"]}`。**没有保存过记录 = 全部拒绝**。只有服务当前
  真实上报的工具才会被保存；重连时清单变化会自动剪掉失效授权项。
- 补充通道：AI 成员配置里直接勾选端侧工具，与闸门 2 取并集。
- 附带效果：有绑定服务在线时，该 AI 自动获得服务端桥接工具 `admin.manage`
  （枚举当前连接的执行端）。

**实施提示**：两道闸门在网页控制台作坊面板都有 UI，正常应由人操作。你完成部署后，
在交接说明中明确提示用户："去作坊面板给本服务分配 AI，并在 MCP 权限中勾选工具"。
仅当用户明确授权时才代为调用上述 REST。

---

## 8. 任务协议：接收调用、回报结果

### 8.1 服务器 → 服务：`task:dispatch`

```jsonc
{
  "taskId": "atask_9f2c01ab34de",   // 回包必须原样带回
  "userId": 1,
  "aiConfigId": 3,                  // 发起调用的 AI
  "sessionId": "sess_...",          // 关联聊天会话（可能为空）
  "instruction": "Run endpoint MCP tool order.query",
  "tool": "order.query",
  "args": {"order_id": "SO-2026-0001"},   // 按你的 input_schema 传入
  "allowedTools": ["order.query"]
}
```

### 8.2 服务 → 服务器：三种回包

| 事件 | 载荷 | 说明 |
| --- | --- | --- |
| `task:result` | `{"taskId", "deviceId", "success", "tool", "result", "summary"}` | `result` 任意可 JSON 序列化值；`summary` 一句话人话总结（展示给用户/模型） |
| `task:error` | `{"taskId", "deviceId", "error"}` | 失败终态（等价 success=false） |
| `task:progress` | `{"taskId", "deviceId", "message"}` | 可选，长任务中途进度，实时推送到网页 |

### 8.3 队列与超时（硬规则）

- **每个任务必须恰好回一次 result 或 error**。收到不认识的 `tool` 回
  `task:error`，不要沉默。
- **每个服务一条串行队列**：前一个任务出终态（result/error/超时）才派发下一个。
  不回包会卡死队列，直到服务器超时兜底（默认 120 秒；孤儿任务最长 5 分钟清扫）。
- 调用默认等 **120 秒**；模型可传 `args.timeout_seconds`（上限 300）延长。
  可能超 120 秒的工具，要在描述里写明"请传 timeout_seconds"。
- 掉线重连后服务器按序重放排队任务；等待过久的会被主动作废。

### 8.4 返回图片的约定

工具名为 `screen.capture` / `screen.capture_region` / `vision.capture` /
`vision.capture_mouse`，或 result 带 `send_to_user: true` 时：result 里的
`dataUrl`（`data:image/png;base64,...`）会被服务器持久化并作为图片发给用户。
自建服务的截图/拍照/图表类工具可复用 `screen.capture` 名字或带
`send_to_user: true`。注意 20 MB 单帧上限。

---

## 9. 远程连接：画面远程 + 命令行远程（可选，默认不实施）

> 仅当项目运行在"有屏幕或能开 shell 的主机"且用户明确要求时才实施本节。
> 纯业务型服务直接跳过。这不是 AI 调工具，而是**真人操作者**在网页控制台实时
> 驱动服务所在主机的独立数据面。

| | **画面远程**（screen） | **命令行远程**（terminal） |
| --- | --- | --- |
| 用途 | 实时屏幕镜像 + 键鼠注入 | 交互式 shell（ANSI / TUI / Ctrl-C / resize） |
| 能力字（`capabilities`） | `remote_control` | `remote_terminal` |
| 事件前缀 | `rc:*` | `rt:*` |
| 传输 | **WebRTC P2P**（仅 SDP/ICE 信令过服务器） | **Socket.IO relay**（字节流经服务器转发） |
| 需要 TURN | 需要（公网跨 NAT，见 9.4） | 不需要 |
| 官方参考实现 | 服务端 `connector_runtime/dispatch/remote_control.py`；Windows `src-tauri/src/rc.rs` + `src/remote-control.ts` | 服务端 `connector_runtime/dispatch/remote_terminal.py`；Windows `src-tauri/src/pty.rs` + `src/remote-terminal.ts` |

在 `capabilities` 里声明哪个能力字就解锁哪条通道；都不声明就都不开。这两个
能力字是**传输层保留字，不是 MCP 工具**（见 5.1）。

### 9.1 会话所有权闸门（两通道通用）

开会话时服务器统一校验：① 控制端（网页）用同一套用户 JWT（放 `rc:start` /
`rt:open` 的 `token` 字段）；② 目标是该用户名下的在线服务；③ 该服务声明了对应
能力字。不满足则回 `rc:error` / `rt:error`（`code`：`unauthorized` / `offline` /
`forbidden` / `unsupported`）。会话按 `sessionId` 存服务器内存，任一方断线即清理。

### 9.2 命令行远程协议：`rt:*`

低带宽字节流经服务器 relay。`data` 一律是 **PTY 原始字节的 base64**（让 ANSI
控制序列原样穿过 JSON，服务器只转发不解码）。

```
控制端（web） → 服务器 → 服务
    rt:open    {deviceId, token, shell?, cols?, rows?, cwd?}
    rt:input   {sessionId, data}          写入 PTY（base64）
    rt:resize  {sessionId, cols, rows}
    rt:close   {sessionId}

服务 → 服务器 → 控制端
    rt:data    {sessionId, data}          PTY 输出（base64）
    rt:exit    {sessionId, code}          shell 退出（code 可为 null）
    rt:error   {sessionId, code, message}

服务器 → 控制端
    rt:opened  {sessionId, deviceId, shell}   受理后才开始发 rt:input
    rt:error   {code, message}
```

服务侧实现要点：收到 `rt:open` 按 `shell`/`cols`/`rows`/`cwd` 起 PTY（Windows
ConPTY，Linux/macOS openpty）→ 持续读输出发 `rt:data`，退出发 `rt:exit` →
`rt:input` 解 base64 写入，`rt:resize` 调行列，`rt:close` 杀进程 → 支持多会话
（按 `sessionId` 路由）→ socket 断线时杀掉全部 PTY。
**安全**：这是该用户对主机的完整 shell（服务器已做属主校验），PTY 应以不超出
预期的权限与工作目录启动。

### 9.3 画面远程协议：`rc:*`

高带宽视频走 WebRTC 点对点，仅信令过服务器：

```
控制端（web） → 服务器 → 服务
    rc:start   {deviceId, token}
    rc:answer  {sessionId, sdp}
    rc:ice     {sessionId, candidate}
    rc:stop    {sessionId}

服务 → 服务器 → 控制端
    rc:offer   {sessionId, sdp}           服务侧发起 offer
    rc:ice     {sessionId, candidate}
    rc:ready   {sessionId, width, height, rotation}
    rc:error / rc:stopped

服务器 → 控制端
    rc:started {sessionId, deviceId}
    rc:error   {code, message}
```

服务侧负责：屏幕采集成 WebRTC 视频轨 + `control` DataChannel 接收归一化到
`[0,1]` 的鼠标/键盘事件并注入本机 OS。实现成本远高于命令行远程，
**多数第三方服务只接命令行远程即可**。

### 9.4 TURN

命令行远程走 relay，公网直接可用。画面远程是 P2P：纯 STUN 在公网跨 NAT 常失败，
需部署 TURN 中继（房主在网页管理控制台「远程控制（STUN/TURN）」卡片填凭据）。
服务侧从登录响应或 `GET /api/rtc/ice-servers` 取 ICE 配置，**不要写死**。

### 9.5 实施清单

- 只要命令行远程：`capabilities` 加 `remote_terminal`，实现 9.2 + 本机 PTY。
- 要画面远程：`capabilities` 加 `remote_control`，实现 9.3 + 按 9.4 取 ICE 配置。
- 两条通道与第 8 节任务循环互不干扰：不进任务队列、不入库、不走聊天管线。

---

## 10. 实施模板（Node.js）

结构与第 2 节 Python 模板一致（登录 → 连接注册 → HANDLERS 路由）：

```js
// npm i socket.io-client axios
const { io } = require('socket.io-client')
const axios = require('axios')

const SERVER = process.env.HEYSURE_SERVER || 'http://127.0.0.1:3000'
const SERVICE_ID = process.env.HEYSURE_SERVICE_ID || 'myproject-01'

const TOOLS = [{
  name: 'report.daily_summary',
  description: '汇总昨日业务数据（订单量、销售额、新增用户）',
  input_schema: { type: 'object', properties: {}, required: [] },
}]
const HANDLERS = {
  'report.daily_summary': async () => {
    // TODO: 接本项目的真实数据
    const result = { orders: 42, revenue: 8360, new_users: 7 }
    return [result, '昨日 42 单，销售额 8360 元，新增用户 7 人']
  },
}

async function main() {
  const { data: login } = await axios.post(`${SERVER}/api/auth/login`, {
    account: process.env.HEYSURE_ACCOUNT, password: process.env.HEYSURE_PASSWORD,
  })
  const socket = io(login.agent_socket_url || SERVER, { reconnectionDelay: 2000 })

  const register = () => socket.emit('device:register', {
    id: SERVICE_ID, name: process.env.HEYSURE_SERVICE_NAME || '我的项目',
    platform: 'custom-service', deviceType: 'custom',
    token: login.access_token, version: '1.0.0',
    capabilities: TOOLS.map(t => t.name), toolDefs: TOOLS,
  })

  socket.on('connect', register)
  socket.on('device:registered', d => console.log('registered, ai =', d.aiConfigId))
  socket.on('device:register_rejected', d => console.error('rejected:', d.reason))

  socket.on('task:dispatch', async task => {
    try {
      const handler = HANDLERS[task.tool]
      if (!handler) throw new Error(`unknown tool: ${task.tool}`)
      const [result, summary] = await handler(task.args || {})
      socket.emit('task:result', {
        taskId: task.taskId, deviceId: SERVICE_ID,
        success: true, tool: task.tool, result, summary,
      })
    } catch (err) {
      socket.emit('task:error', { taskId: task.taskId, deviceId: SERVICE_ID, error: String(err) })
    }
  })
}
main()
```

---

## 11. 辅助 REST 接口（Bearer `access_token`）

| 接口 | 用途 |
| --- | --- |
| `GET /api/devices/connected` | 当前账号的服务快照（在线 + 离线遗留），验证注册 |
| `POST /api/devices/bind` | 绑定/解绑 AI（`{"deviceId", "aiConfigId"}`，null 解绑） |
| `GET /api/devices/{id}/mcp-scope` | 查看某在线服务的工具清单与已授权子集 |
| `PUT /api/devices/{id}/mcp-scope` | 保存该服务的工具授权清单 |
| `DELETE /api/devices/{id}` | 遗忘一个**离线**服务（删绑定 + presence + 授权） |
| `GET /api/auth/agent-endpoint` | 用现有 token 重新获取 `agent_socket_url` |

---

## 12. 验收与排查

### 12.1 验收步骤（AI 实施完成后逐条执行）

1. 启动接入模块，日志出现"已注册"（收到 `device:registered`）。
2. `curl -H "Authorization: Bearer <token>" {SERVER}/api/devices/connected`
   → 列表中出现你的 `SERVICE_ID`，且工具清单完整。
3. 交接给用户：提示去网页控制台作坊面板 ① 给本服务分配 AI，② 在 MCP 权限中
   勾选工具并保存。
4. 用户对绑定的 AI 发一句会触发工具的话（如"查一下订单 SO-2026-0001"），
   确认 `task:dispatch` 到达、回包成功、AI 回复中含结果。
5. 断网 / 重启服务进程各一次，确认自动重连重注册，绑定与权限无需重配。

### 12.2 排查表

| 症状 | 检查 |
| --- | --- |
| 连上就断 / `device:register_rejected` | token 过期 → 重新登录；reason 字段有具体原因 |
| 注册成功但网页看不到 | 登录账号是否与网页账号相同；`GET /api/devices/connected` 里有没有 |
| 控制台显示"设备端/未知"而非"自定义设备" | 注册包缺 `deviceType: "custom"`；或服务端版本过旧 |
| AI 提示词里看不到工具 | 两道闸门：是否绑定了 AI？MCP 权限是否勾选并保存？ |
| AI 能看到工具但报"no agent connected" | 服务是否在线；`capabilities` 是否含该工具名 |
| 任务派发后一直转圈 | 该 taskId 是否恰好回了一次 `task:result` / `task:error` |
| 后续任务全部排队 | 前一个任务没回终态，卡住串行队列；回包或等超时清扫 |
| 大结果发不出去 | 20 MB 单帧上限；压缩或改回摘要 + 服务器路径 |
| 工具改名/增删后授权丢失 | 预期行为：服务器按当前上报清单剪授权，去面板重新勾选 |
| 控制台建了动态 MCP 工具但调不到 | 预期行为：第三方服务不实现该通道（第 6 节） |
| 命令行远程报"不支持" | `capabilities` 是否声明 `remote_terminal`（第 9 节） |
| 画面远程公网连上就断 | STUN 打洞失败，需 TURN（9.4）；命令行远程无此问题 |
| 命令行远程有回显但输入无效 | `rt:input` 的 `data` 是否 base64 |

---

## 13. 服务器行为契约

服务器对遵循本协议的服务保证：

1. 注册包的 `name` / `deviceType` / `capabilities` / `toolDefs` **原样进入**
   presence 快照，AI 看到的工具目录、描述、schema 以服务自报为准；
2. 自建服务与官方端走**同一条**调度通道（串行队列、超时、重放、结果入库、
   网页实时推送），无功能阉割；
3. 同名工具存在于多个绑定执行端时，优先派发给申报了该工具的那个；
4. 绑定与授权按 `(用户, 服务id)` 持久化，跨重连、跨服务器重启保持；
5. 动态 MCP 定义（第 6 节）只由服务器写入，不实现该通道不影响静态 `toolDefs` 调度；
6. 远程连接两通道（第 9 节）开会话时统一做所有权校验，与任务循环互不干扰。

协议演进以服务端实现为最终权威：`connector_runtime/dispatch/device_dispatch.py`
（任务分发）、`remote_control.py`（`rc:*`）、`remote_terminal.py`（`rt:*`）。
