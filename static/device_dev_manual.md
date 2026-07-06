# HeySure 设备开发手册（read.md）

> 面向**第三方 / 自建设备开发者**：只要遵循本手册的接入协议，任何能跑 Socket.IO 客户端的程序——
> 树莓派、ESP32 网关、NAS 脚本、机器人、智能家居 Hub、另一台服务器上的守护进程——
> 都可以注册为 HeySure 的端侧设备，把自己的能力以 MCP 工具的形式交给 AI 成员调用。
>
> 本目录下的 `windows/` `linux/` `mac/` `extension/` `android/` 是官方内置设备实现，
> 它们走的就是本手册描述的同一套协议，可作为完整参考实现。

---

## 1. 接入模型一图流

```
┌─────────────┐   1. REST 登录换 token           ┌──────────────────────┐
│  你的设备    │ ───────────────────────────────► │  HeySure 服务器       │
│ (任意语言/   │   POST /api/auth/login           │  (API Gateway :3000)  │
│  任意硬件)   │ ◄─────────────────────────────── │                      │
│             │   access_token + agent_socket_url│                      │
│             │                                  │                      │
│             │   2. Socket.IO 长连接 + 注册      │                      │
│             │ ───────────────────────────────► │ device:register      │
│             │ ◄─────────────────────────────── │ device:registered    │
│             │                                  │                      │
│             │   3. 等待任务下发                 │  （网页控制台"作坊"    │
│             │ ◄─────────────────────────────── │   面板绑定 AI + 勾选   │
│             │   task:dispatch {tool, args}     │   工具权限后生效）     │
│             │ ───────────────────────────────► │                      │
│             │   task:result / task:error       │                      │
└─────────────┘                                  └──────────────────────┘
```

要点：

- **设备本身不需要任何 AI 能力**，它只是一个"工具执行器"。推理、编排全在服务端。
- 设备在注册时**自报设备名、设备类型和 MCP 工具清单**（名称 + 描述 + JSON Schema）。
  服务器原样存储这些自描述信息，不做任何硬编码假设——你上报什么，AI 就能看到什么。
- 自建设备注册时声明 `deviceType: "custom"`，服务器把它当作与官方桌面端同级的
  执行端来调度（presence、绑定、权限、任务队列全部通用）。
- 工具是否真正暴露给某个 AI，由两道闸门控制：**设备 ↔ AI 绑定**（作坊面板分配）
  和**设备级工具授权范围**（默认全关，需在面板勾选）。见第 7 节。

---

## 2. 五分钟快速开始（Python）

依赖：`pip install "python-socketio[client]" requests`（Socket.IO 协议 v5 / Engine.IO v4，
对应 JS 端 `socket.io-client` v4.x）。

```python
import socketio
import requests

SERVER = "http://127.0.0.1:3000"          # 你的 HeySure 网关地址
ACCOUNT, PASSWORD = "heysure", "heysure"  # 设备属主账号（和网页控制台同一账号）
DEVICE_ID = "custom-lamp-01"              # 稳定唯一，重连/重启后必须不变

# 1) 登录换取 token 与 Socket.IO 连接地址
login = requests.post(
    f"{SERVER}/api/auth/login",
    json={"account": ACCOUNT, "password": PASSWORD},
    timeout=10,
).json()
TOKEN = login["access_token"]
SOCKET_URL = login.get("agent_socket_url") or SERVER

# 2) 定义本设备提供的 MCP 工具（自描述：名称 + 说明 + 参数 JSON Schema）
TOOLS = [
    {
        "name": "lamp.set_state",
        "description": "打开或关闭书房台灯",
        "input_schema": {
            "type": "object",
            "properties": {
                "on": {"type": "boolean", "description": "true=开灯, false=关灯"},
            },
            "required": ["on"],
        },
    },
    {
        "name": "lamp.get_state",
        "description": "查询书房台灯当前开关状态",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]

sio = socketio.Client(reconnection=True, reconnection_delay=2)
registered = False

def register():
    sio.emit("device:register", {
        "id": DEVICE_ID,
        "name": "书房台灯",
        "platform": "esp32-lamp-gateway",
        "deviceType": "custom",          # ← 自建设备的关键声明
        "icon": "1",                     # 可选：预置图标编号（见 4.1），不填走网页默认
        "token": TOKEN,
        "version": "1.0.0",
        "lifecycle": "registered",
        "capabilities": [t["name"] for t in TOOLS],
        "toolDefs": TOOLS,
    })

@sio.event
def connect():
    register()                            # 每次(重)连接都要重新注册

@sio.on("device:registered")
def on_registered(data):
    global registered
    registered = True
    print("已注册；当前绑定的 AI:", data.get("aiConfigId") or "（未绑定，去网页作坊面板分配）")

@sio.on("device:register_rejected")
def on_rejected(data):
    print("注册被拒绝:", data.get("reason"))  # 常见原因：token 过期 → 重新登录

@sio.on("task:dispatch")
def on_task(task):
    task_id = task.get("taskId")
    tool = task.get("tool")
    args = task.get("args") or {}
    try:
        if tool == "lamp.set_state":
            # TODO: 在这里真正控制你的硬件
            result = {"on": bool(args.get("on"))}
            summary = "台灯已" + ("打开" if args.get("on") else "关闭")
        elif tool == "lamp.get_state":
            result = {"on": True}
            summary = "台灯当前为开启状态"
        else:
            raise ValueError(f"本设备不支持工具: {tool}")
        sio.emit("task:result", {
            "taskId": task_id, "deviceId": DEVICE_ID,
            "success": True, "tool": tool,
            "result": result, "summary": summary,
        })
    except Exception as exc:
        sio.emit("task:error", {
            "taskId": task_id, "deviceId": DEVICE_ID, "error": str(exc),
        })

sio.connect(SOCKET_URL)
sio.wait()
```

跑起来之后，到网页控制台（作坊面板）里会看到一台名为"书房台灯"的**自定义设备**：

1. 给它**分配一个 AI 成员**（绑定）；
2. 打开它的 **MCP 权限编辑**，勾选允许 AI 使用的工具并保存；
3. 对该 AI 说"帮我开一下书房的台灯"，`lamp.set_state` 就会被派发到你的进程。

---

## 3. 获取连接信息

### 3.1 登录换 token

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

- `access_token`：用户 JWT，注册时放进 `device:register` 的 `token` 字段。
  设备与网页控制台共用同一套账号体系——**设备属于登录它的那个用户**。
- `agent_socket_url`：设备应连接的 Socket.IO 基地址（服务器可能通过
  `AGENT_SOCKET_URL` 环境变量显式配置；未配置时按登录请求的 host 推导）。
  **永远优先使用该字段**，不要写死端口。
- token 有效期由服务端 JWT 配置决定。收到 `device:register_rejected`
  且 reason 提示 token 无效时，重新调 `/api/auth/login` 换新 token 再注册。
- 已持有 token 时也可以用 `GET /api/auth/agent-endpoint`（Bearer 认证）
  单独刷新 `agent_socket_url`。

### 3.2 Socket.IO 连接参数

| 参数 | 值 |
| --- | --- |
| URL | 登录响应里的 `agent_socket_url` |
| path | `/socket.io/`（默认值，勿改） |
| 命名空间 | 默认命名空间 `/` |
| 协议版本 | Socket.IO v4（JS `socket.io-client` ≥ 4.x / `python-socketio` ≥ 5.x） |
| 传输 | 允许 polling → websocket 自动升级（反代已配置 WS 升级） |
| 单帧上限 | 20 MB（服务端 `max_http_buffer_size`，大图请压缩或分片） |

---

## 4. 注册协议：`device:register`

连接（含每次重连）成功后立即发送。**在收到 `device:registered` 之前建议每 3 秒
重发一次**（官方桌面端就是这么做的），防止注册包在升级握手期间丢失。

```jsonc
{
  // ── 必填 ────────────────────────────────────────────────
  "id": "custom-lamp-01",        // 逻辑设备 ID：稳定、全局唯一（建议 <型号>-<序列号>）。
                                 // 绑定、权限、任务队列全部按它落库，重连/重启后必须不变。
  "name": "书房台灯",             // 展示名：网页面板与 AI 看到的设备名
  "token": "<access_token>",     // 登录得到的用户 JWT（服务器校验后即时删除，不落库）
  "deviceType": "custom",        // 自建设备固定填 "custom"。
                                 // 填其它未知字符串也会被归一化为 custom；
                                 // 但请不要冒充内置类型（desktop/browser/android/workshop）。

  // ── 强烈建议 ─────────────────────────────────────────────
  "platform": "esp32-lamp-gateway",  // 自由字符串，描述运行环境。
                                     // 避免包含 desktop / windows / browser / android /
                                     // workshop 这些词（旧版服务器按关键词分类）。
  "capabilities": ["lamp.set_state", "lamp.get_state"],  // 工具名清单
  "toolDefs": [ /* 见第 5 节 */ ],   // 工具自描述（缺失的工具会得到"无 schema"的宽松兜底）
  "version": "1.0.0",               // 你的固件/程序版本，仅展示用

  // ── 可选 ────────────────────────────────────────────────
  "icon": "3",                   // 设备图标（见 4.1）：预置编号 / /device_png/ 路径 /
                                 // 绝对 http(s) URL。不填 = 网页默认样式
  "lifecycle": "registered",     // 生命周期标签，默认 "registered"
  "group": "",                   // 分组标签，仅展示
  "os": {"platform": "linux", "arch": "arm64", "hostname": "pi-01"}  // 仅展示
}
```

服务器回应（发给当前 socket）：

| 事件 | 载荷 | 含义 |
| --- | --- | --- |
| `device:registered` | `{"id", "aiConfigId"}` | 注册成功。`aiConfigId` 为服务端已持久化的 AI 绑定（null = 尚未分配，绿灯/黄灯指示可据此显示） |
| `device:register_rejected` | `{"reason"}` | 注册被拒。典型原因：token 缺失/过期、绑定的 AI 不属于该用户 |

注册成功后服务器会：

1. 把设备写入**在线 presence 快照**（含设备名、类型、工具清单与 schema），
   四个后端进程共享这份快照做工具发现；
2. 恢复该设备上**积压的任务队列**（掉线期间排队的任务按序重放）;
3. 向属主用户的网页推送最新设备列表（`device:list`）。

### 4.1 设备图标（可选）

服务器预置了一组设备图标，挂载在网关的 `/device_png/` 下（当前为
`1.webp` ~ `8.webp`，浏览器直接访问 `{SERVER}/device_png/3.webp` 可预览）。
注册包的 `icon` 字段接受三种写法：

| 写法 | 示例 | 归一化结果 |
| --- | --- | --- |
| 预置编号 | `"3"` 或 `"3.webp"` | `/device_png/3.webp` |
| 预置路径 | `"/device_png/3.webp"` | 原样 |
| 绝对 URL | `"https://example.com/my-icon.png"` | 原样（需可被浏览器访问） |

- 不填、填空串或格式不合法 → 归一化为空，**网页控制台按内置默认样式渲染**。
- 图标随 presence 持久化：设备离线后面板里仍显示所选图标。
- 换图标：修改 `icon` 后重新 `device:register` 即可（重连自动生效）。

注意事项：

- **设备不能自选 AI**。绑定关系只由操作者在网页作坊面板分配，注册时服务器
  自动套用已持久化的绑定。
- **同一 AI 每种设备类型最多绑一台**。一个 AI 可以同时绑定桌面端 + 浏览器 +
  安卓 + 自定义设备各一台；想接多台自定义设备就绑到不同 AI。
- 掉线后重连：用同一个 `id` 重新 `device:register` 即可，绑定与权限自动恢复。

---

## 5. 工具自描述：`toolDefs`

`toolDefs` 是一个数组，每个元素描述一个工具。**设备是自己工具 schema 的唯一
权威**——服务器原样存储并转交给模型，不会替你编造描述。

```jsonc
{
  "name": "lamp.set_state",            // 必填，与 capabilities 中的名字一致
  "description": "打开或关闭书房台灯。on=true 开灯。",  // 必填（对模型可见，写清楚！）
  "input_schema": {                    // 必填，标准 JSON Schema（也接受 inputSchema 拼写）
    "type": "object",
    "properties": {
      "on": {"type": "boolean", "description": "true=开灯"}
    },
    "required": ["on"]
  },
  "destructive": false                 // 可选：标记危险/不可逆操作（用于 UI 提示）
}
```

### 5.1 工具命名规范

- 推荐格式：`<域>.<动作>`，全小写，如 `lamp.set_state`、`printer.print_file`、
  `cam.snapshot`。域名用你的设备品类，**在同一账号下保持唯一**，避免与其它
  设备的工具重名（重名时服务器优先派发给真正申报了该工具的设备，但唯一命名
  始终是最稳妥的）。
- **保留前缀，禁止使用**（它们会被服务器归类到别的通道或直接剥离）：
  - `browser_` / `card_` —— 浏览器扩展通道专用
  - `evolution.` / `librarian.` —— 知识与进化工坊专用
  - `remote_control` / `remote.control` —— 画面远程能力保留字（不是可调用工具，见第 9 节）
  - `remote_terminal` / `remote.terminal` —— 命令行远程能力保留字（同上，见第 9 节）
  - `rc:` / `rt:` —— 远程连接的 Socket.IO 事件前缀，勿用作工具名
- 只出现在 `capabilities` 而没有 `toolDefs` 条目的工具也能被调用，但模型只能
  盲传参数（服务器为它兜底一个"任意对象"的宽松 schema）。**始终写全 toolDefs。**

### 5.2 描述怎么写才好用

`description` 是模型决定"何时调用、怎么传参"的唯一依据，写作建议：

- 一句话说清工具做什么 + 关键参数含义 + 明显的限制（"仅支持 PNG"、"耗时约 10s"）。
- 有副作用/不可逆的操作在描述里写明，并设 `destructive: true`。
- 参数在 `input_schema.properties.*.description` 里逐个解释，必填项进 `required`。

---

## 6. 动态 MCP：服务器下发的工具（可选，与 toolDefs 是两回事）

第 5 节的 `toolDefs` 是**设备自己**声明的固定工具——写死在你的程序里，改工具
=改代码=重新发布。HeySure 官方桌面端（Windows/Linux/macOS）和浏览器扩展还支持
**另一条完全独立的通道**：AI 或人类在服务器这边**运行时**给某个用户名下的某种
设备类型编写新工具，服务器把定义原样推给当前在线的设备去执行。这条通道和
`toolDefs` 是两回事，很多接入方会把两者混为一谈，这里把边界讲清楚。

|  | 静态 `toolDefs`（第 5 节） | 动态 MCP（本节） |
| --- | --- | --- |
| 定义存在哪 | 设备自己的代码/固件里 | 服务器（按用户 + 设备类型存储） |
| 谁能改 | 只有设备开发者，改完要重新发布/重启设备 | 已绑定"图书馆"的 AI（`device_mcp.manage` 工具）或人在网页控制台编辑，**设备自己不能改** |
| 何时生效 | 设备重新 `device:register` 时 | 服务器实时推送，在线设备立刻应用 |
| 设备要做什么 | 老实执行自己写的逻辑 | 接收定义、合并进本地工具目录、**安全执行**服务器给的代码 |
| 第三方自建设备要不要实现 | **要**（这是接入的基本要求） | **不需要**，可以完全忽略 |

### 6.1 谁在写、写到哪

只有已绑定"图书馆"（知识工坊）的 AI，或登录同一账号的人在网页控制台的"设备
动态 MCP"面板，才能创建/修改/删除这批定义。定义按 `(用户, 设备类型)` 存在
服务器一侧，**设备本身没有持久化这份数据的能力，也没有任何"本地新建工具"的
入口或 MCP 管理工具**——设备是纯粹的执行端，看到什么就跑什么，仅此而已。

### 6.2 服务器 → 设备：`device:tool-config`

服务器通过 Socket.IO 事件把当前定义整体推给设备（不是增量 diff）：

- **推送时机**：① 每次 `device:register` 成功后，服务器立即补发该设备类型
  当前的完整定义集合（掉线期间的编辑不会错过）；② 之后只要有人再编辑一次，
  服务器实时把新的整体集合再推给所有在线的同类型设备。
- **载荷**：

```jsonc
{
  "tools": [
    {
      "name": "fs.read_better",              // 与 toolDefs 一样：小写点分域名
      "description": "...",                  // 同样是模型决定何时调用的唯一依据
      "input_schema": { "type": "object", "properties": {} },
      "code_kind": "js",                     // "program" | "js" | "runtime"
      "js": "return await cap.call('fs.read', args)"   // code_kind=js 时的函数体
      // code_kind=program 时改用 "code": [{op:'call'|'set'|'return', ...}, ...]
      // code_kind=runtime 时改用 "runtime": "powershell"|"shell"|"python" + "source": "..."
      // 注意 runtime 可用值按平台而定：Windows 只认 powershell/shell（无 python）；
      // Linux/macOS 三者都支持。定义了目标平台不支持的 runtime 会在该设备上校验失败。
    }
  ],
  "permissionPolicy": { "...": "..." }         // 可选：runtime 工具的权限策略，随包下发
}
```

- **设备收到后应该做的事**（官方桌面端 `executor/dynamic.ts` 的实现方式，仅供
  参考，第三方设备可以用任何等价方式）：校验定义 → 合并进本地工具路由表
  （用名字覆盖同名条目）→ 重新执行一次 `device:register`，把合并后的
  `capabilities`/`toolDefs` 重新上报，AI 才知道这些工具现在真的可调用；
  断线时清空这批定义（避免离线期间过期的定义被误当作仍然有效）。
- **执行安全**：`js`/`program` 两种实现方式要求设备能够**安全沙箱执行服务器
  下发的任意代码**（官方客户端用注入受限作用域的 `AsyncFunction` /
  call-set-return 微型 DSL）；`runtime` 方式则是把源码转发给设备本机的
  shell/PowerShell/Python 解释器执行，按权限标签做 allow/confirm/deny。这正是
  为什么这条通道只由官方富客户端和浏览器扩展实现——**第三方自建设备完全可以
  不接这个事件**，第 5 节的静态 `toolDefs` 才是本手册对接入方的基本要求。
- **调用协议不变**：无论工具来自 `toolDefs` 还是这里的动态下发，AI 发起调用
  时走的都是第 8 节同一套 `task:dispatch` / `task:result`——服务器只按工具名
  派发，不区分来源，设备也不需要为"动态工具"另写一套回包逻辑。

---

## 7. 绑定与授权：工具什么时候真正可用

设备在线 ≠ AI 能用它的工具。链路上有两道闸门，全部默认关闭：

```
设备在线（presence）
   └─► 闸门 1：设备 ↔ AI 绑定        （作坊面板"分配 AI"；POST /api/devices/bind）
          └─► 闸门 2：设备级工具授权   （作坊面板"MCP 权限"勾选；PUT /api/devices/{id}/mcp-scope）
                 └─► AI 的系统提示词中出现该工具，可发起调用
```

- **闸门 1（绑定）**：`POST /api/devices/bind`，body
  `{"deviceId": "custom-lamp-01", "aiConfigId": 3}`（null 解绑）。绑定按
  `(用户, 设备id)` 持久化，重连自动恢复；设备离线时也可以预先绑定，下次上线生效。
- **闸门 2（授权范围）**：每台设备有独立的工具允许清单，**没有保存过记录 =
  全部拒绝**。`PUT /api/devices/{deviceId}/mcp-scope`，body
  `{"tools": ["lamp.set_state", "lamp.get_state"]}`。只有设备当前真实上报的
  工具才会被保存（防止越权）。设备重连时若工具清单变化，服务器自动剪掉
  已不存在的授权项。
- 补充通道：在 AI 成员配置的 MCP 工具清单里直接勾选端侧工具，效果与闸门 2
  取并集（只要有在线设备申报该工具即放行）。
- 额外赠品：只要有绑定的端侧设备在线，该 AI 自动获得服务端桥接工具
  `admin.manage`（可枚举当前连接的设备）。

以上操作在网页控制台作坊面板全部有对应 UI，通常不需要设备开发者手工调 REST。

---

## 8. 任务协议：接收调用、回报结果

### 8.1 服务器 → 设备：`task:dispatch`

```jsonc
{
  "taskId": "atask_9f2c01ab34de",   // 本次调用唯一 ID，回包必须原样带回
  "userId": 1,
  "aiConfigId": 3,                  // 发起调用的 AI
  "sessionId": "sess_...",          // 关联的聊天会话（可能为空）
  "instruction": "Run endpoint MCP tool lamp.set_state",
  "tool": "lamp.set_state",         // 要执行的工具名
  "args": {"on": true},             // 按你的 input_schema 传入的参数
  "allowedTools": ["lamp.set_state"]
}
```

### 8.2 设备 → 服务器：三种回包

| 事件 | 载荷 | 说明 |
| --- | --- | --- |
| `task:result` | `{"taskId", "deviceId", "success": true/false, "tool", "result", "summary"}` | **每个任务必须恰好回一次** result 或 error。`result` 任意可 JSON 序列化值；`summary` 一句话人话总结（会展示给用户/模型） |
| `task:error` | `{"taskId", "deviceId", "error"}` | 执行失败（等价于 success=false 的终态） |
| `task:progress` | `{"taskId", "deviceId", "message"}` | 可选，长任务的中途进度，会实时推送到网页 |

### 8.3 队列与超时（务必遵守）

- **每台设备一条串行队列**：服务器一次只派发一个任务，前一个任务收到终态
  （result/error/超时）后才派发下一个。**不回包会卡住整台设备的队列**，
  直到服务器超时兜底（默认 120 秒，孤儿任务最长 5 分钟被清扫）。
- 调用方默认等待 **120 秒**；模型可通过 `args.timeout_seconds`（上限 300）
  申请更长等待。你的工具如果可能超过 120 秒，在工具描述里写明"请传
  timeout_seconds"。
- 掉线重连后，服务器会自动把排队中的任务按序重放给你；等待过久的排队任务
  会被服务器主动作废（避免"几分钟前的点击"突然重放）。
- 收到不认识的 `tool` 时，回 `task:error` 而不是沉默。

### 8.4 返回图片/截图的约定

工具名恰好是 `screen.capture` / `screen.capture_region` / `vision.capture` /
`vision.capture_mouse`（或返回体带 `send_to_user: true`）时，`result` 里的
`dataUrl` 字段（`data:image/png;base64,...`）会被服务器持久化并作为图片
发给用户。自建设备的拍照/截图类工具可复用 `screen.capture` 这个名字获得
该待遇，或在 result 里带 `send_to_user: true`。注意 20 MB 单帧上限。

---

## 9. 远程连接：画面远程 + 命令行远程（统一标准）

前面第 8 节讲的是 **AI 任务循环**（`task:dispatch`：AI 编排、串行队列、结果入库）。
**远程连接是另一条完全独立的实时数据面**：由**真人操作者**在网页控制台里实时
驱动一台设备——不是 AI 调工具。它有两种形态，共享同一套「会话」模型，只是
**传输不同**：

| | **画面远程**（screen） | **命令行远程**（terminal） |
| --- | --- | --- |
| 用途 | 实时屏幕镜像 + 键鼠注入 | 交互式 shell（ANSI 颜色 / TUI / Ctrl-C / 窗口 resize） |
| 能力字（`capabilities`） | `remote_control` | `remote_terminal` |
| 事件前缀 | `rc:*` | `rt:*` |
| 传输 | **WebRTC P2P**（视频轨 + `control` DataChannel），只有 SDP/ICE 信令过服务器 | **Socket.IO relay**：字节流本身经服务器转发，无 WebRTC |
| 是否需要 TURN | **需要**（跨公网 NAT 打洞，见 9.4） | **不需要**（文本量小，直连中继即可，公网可用） |
| 谁发起 SDP | 设备 offer，浏览器 answer | 无 SDP（不是 WebRTC） |
| 官方参考实现 | 服务端 `connector_runtime/dispatch/remote_control.py`；Windows `src-tauri/src/rc.rs` + `src/remote-control.ts` | 服务端 `connector_runtime/dispatch/remote_terminal.py`；Windows `src-tauri/src/pty.rs` + `src/remote-terminal.ts` |

两者都是**可选**能力：设备在 `device:register` 的 `capabilities` 里声明哪个，
就解锁哪条通道；两个都不声明就都不开。`remote_control` / `remote_terminal`
（含点分写法 `remote.control` / `remote.terminal`）是**传输层保留字，不是
AI 可调用的 MCP 工具**——服务器会把它们从工具目录里剥掉（见 5.1）。

### 9.1 会话所有权闸门（两通道通用）

无论 `rc:*` 还是 `rt:*`，服务器在**开会话时**做同一套校验（`start_session` /
`open_session`）：

1. 控制端（网页）用**同一套用户 JWT** 证明身份（放在 `rc:start` / `rt:open`
   的 `token` 字段）；
2. 目标设备必须是**该用户名下的在线设备**；
3. 且该设备**声明了对应能力**（`remote_control` / `remote_terminal`）。

任何一条不满足，服务器直接回 `rc:error` / `rt:error`（带 `code`：`unauthorized`
/ `offline` / `forbidden` / `unsupported`），不会把会话建起来。会话按
`sessionId` 存在服务器内存里，控制端或设备**任一方断线**都会被清理（画面远程
让设备停止采集；命令行远程杀掉对应 PTY）。

### 9.2 命令行远程协议：`rt:*`

低带宽字节流，**直接经服务器 relay**（`server → 另一端`），因此天生不依赖 TURN。
`data` 字段是**「PTY 原始字节的 base64」**——base64 是为了让 ANSI/光标/控制序列
原样穿过 JSON，服务器只做转发不解码。

```
控制端（web） → 服务器 → 设备
    rt:open    {deviceId, token, shell?, cols?, rows?, cwd?}   开会话
    rt:input   {sessionId, data}      键入写进 PTY（data=base64 字节）
    rt:resize  {sessionId, cols, rows}   窗口尺寸变化，让 TUI 重排
    rt:close   {sessionId}            关闭会话

设备 → 服务器 → 控制端
    rt:data    {sessionId, data}      PTY 输出（data=base64 字节）
    rt:exit    {sessionId, code}      shell 进程退出（code 可能为 null）
    rt:error   {sessionId, code, message}   本端失败

服务器 → 控制端
    rt:opened  {sessionId, deviceId, shell}   会话已受理（此时才开始发 rt:input）
    rt:error   {code, message}                开会话被拒
```

**设备侧收到 `rt:open` 后应做的事**（官方 Windows 端 `pty.rs` + `remote-terminal.ts`
的做法，第三方可用任意等价方式）：

1. 按 `shell`（如 `powershell` / `pwsh` / `cmd`，缺省自选一个交互 shell）、
   `cols`/`rows`、`cwd` 起一个**伪终端（PTY）**（Windows 走 ConPTY，
   Linux/macOS 走 openpty）；
2. 后台读 PTY 输出 → base64 → 持续发 `rt:data`；进程退出时发 `rt:exit`
   带退出码；
3. 收到 `rt:input` 把 base64 解码写进 PTY；收到 `rt:resize` 调整 PTY 行列；
   收到 `rt:close` 杀掉进程回收会话；
4. **一台设备可并存多个会话**（按 `sessionId` 路由）；agent socket 断线时把
   自己所有 PTY 全部杀掉，避免留下孤儿 shell。

> **安全**：命令行远程给的是**该用户对自己设备的完整 shell**，等价于本机
> 终端，请确保只对设备属主开放（服务器已在 9.1 做所有权校验）。设备侧应把
> PTY 跑在合适的工作目录/权限下，不要以超出预期的权限启动。

### 9.3 画面远程协议：`rc:*`

高带宽实时视频，走 **WebRTC 点对点**，只有 SDP/ICE 这几条小信令过服务器，
**视频与键鼠事件本身不碰服务器**：

```
控制端（web） → 服务器 → 设备
    rc:start   {deviceId, token}       开会话
    rc:answer  {sessionId, sdp}        对 offer 的 SDP answer
    rc:ice     {sessionId, candidate}  trickle ICE
    rc:stop    {sessionId}             结束

设备 → 服务器 → 控制端
    rc:offer   {sessionId, sdp}        SDP offer（设备 offer）
    rc:ice     {sessionId, candidate}  trickle ICE
    rc:ready   {sessionId, width, height, rotation}
    rc:error / rc:stopped

服务器 → 控制端
    rc:started {sessionId, deviceId}   会话已受理
    rc:error   {code, message}         开会话被拒
```

**设备侧**负责：把屏幕采集成一条 WebRTC 视频轨（官方桌面端用**原生抓屏**
→ canvas.captureStream，绕开 `getDisplayMedia` 的「屏幕共享」弹窗），开一个
`control` DataChannel 接收归一化到 `[0,1]` 的鼠标/键盘事件并注入本机 OS。
第三方设备若要接画面远程，需自备 WebRTC 端点与输入注入实现——比命令行远程
重很多，**多数第三方设备只接命令行远程即可**。

### 9.4 传输选型与 TURN（为什么两条通道不一样）

- **命令行远程走 relay、不需要 TURN**：终端是文本，带宽极低，直接让服务器
  在两端之间转发字节最简单也最稳——操作端与设备处在不同 NAT / 云安全组 /
  CGNAT 下都能用。
- **画面远程走 P2P、需要 TURN**：~30fps 视频太重，不能过服务器，只能点对点。
  纯 STUN 打洞在同局域网/同机可用，但**公网跨 NAT 常常失败**，需要部署
  **TURN 中继**。服务器会把 ICE 配置（STUN/TURN）随登录响应与
  `GET /api/rtc/ice-servers` 下发，房主在**网页管理控制台 →「远程控制
  （STUN/TURN）」**卡片填 TURN 凭据；设备侧只需照单使用，别写死。

### 9.5 第三方设备接入清单

- 只想要命令行远程：`capabilities` 加 `remote_terminal`，实现 9.2 的
  `rt:*` 收发 + 本机 PTY 即可，无需任何 WebRTC。
- 想要画面远程：`capabilities` 加 `remote_control`，实现 9.3 的 WebRTC 端点
  与输入注入，并照 9.4 使用服务器下发的 ICE 配置。
- 两条通道与第 8 节的 `task:dispatch` **互不干扰**，可同时存在；它们不进
  任务队列、不入库、不走聊天管线。

---

## 10. Node.js 最小示例

```js
// npm i socket.io-client axios
const { io } = require('socket.io-client')
const axios = require('axios')

const SERVER = 'http://127.0.0.1:3000'
const DEVICE_ID = 'custom-nas-01'
const TOOLS = [{
  name: 'nas.disk_usage',
  description: '查询 NAS 各磁盘分区使用率',
  input_schema: { type: 'object', properties: {}, required: [] },
}]

async function main() {
  const { data: login } = await axios.post(`${SERVER}/api/auth/login`,
    { account: 'heysure', password: 'heysure' })
  const socket = io(login.agent_socket_url || SERVER, { reconnectionDelay: 2000 })

  const register = () => socket.emit('device:register', {
    id: DEVICE_ID, name: '家里的 NAS', platform: 'truenas-scale',
    deviceType: 'custom', token: login.access_token, version: '1.0.0',
    capabilities: TOOLS.map(t => t.name), toolDefs: TOOLS,
  })

  socket.on('connect', register)
  socket.on('device:registered', d => console.log('registered, ai =', d.aiConfigId))
  socket.on('device:register_rejected', d => console.error('rejected:', d.reason))

  socket.on('task:dispatch', async task => {
    try {
      if (task.tool !== 'nas.disk_usage') throw new Error(`unknown tool ${task.tool}`)
      const result = { pools: [{ name: 'tank', used_pct: 63 }] } // TODO: 真实实现
      socket.emit('task:result', {
        taskId: task.taskId, deviceId: DEVICE_ID,
        success: true, tool: task.tool, result, summary: 'tank 池已用 63%',
      })
    } catch (err) {
      socket.emit('task:error', { taskId: task.taskId, deviceId: DEVICE_ID, error: String(err) })
    }
  })
}
main()
```

---

## 11. 辅助 REST 接口（Bearer `access_token` 认证）

| 接口 | 用途 |
| --- | --- |
| `GET /api/devices/connected` | 当前账号的设备快照（在线 + 离线遗留记录），调试注册是否成功 |
| `POST /api/devices/bind` | 绑定/解绑 AI（`{"deviceId", "aiConfigId"}`，null 解绑） |
| `GET /api/devices/{id}/mcp-scope` | 查看某在线设备的工具清单与已授权子集 |
| `PUT /api/devices/{id}/mcp-scope` | 保存该设备的工具授权清单 |
| `DELETE /api/devices/{id}` | 遗忘一台**离线**设备（删除绑定 + presence + 授权记录） |
| `GET /api/auth/agent-endpoint` | 用现有 token 重新获取 `agent_socket_url` |

---

## 12. 排查表

| 症状 | 检查 |
| --- | --- |
| 连上就断 / 收到 `device:register_rejected` | token 是否过期 → 重新登录；reason 字段有具体原因 |
| 注册成功但网页看不到设备 | 登录账号是否与网页账号相同；`GET /api/devices/connected` 里有没有 |
| 设备显示"设备端/未知"而不是"自定义设备" | 注册包里 `deviceType: "custom"` 是否携带；服务端是否为支持 custom 的版本 |
| AI 提示词里看不到工具 | 两道闸门：作坊面板是否绑定了 AI？MCP 权限是否勾选并保存？ |
| AI 能看到工具但调用报"no agent connected" | 设备是否在线；`capabilities` 是否包含该工具名 |
| 任务派发下来但一直转圈 | 你是否对每个 taskId 恰好回了一次 `task:result` / `task:error` |
| 后续任务全部排队不执行 | 前一个任务没回终态，卡住了串行队列；回包或等超时清扫 |
| 大结果发不出去 | 单帧 20 MB 上限；压缩图片或改为返回摘要 + 服务器路径 |
| 工具改名/增删后授权丢失 | 正常行为：服务器按当前上报清单自动剪授权，去面板重新勾选 |
| AI/网页说已创建动态 MCP 工具，设备侧一直调不到 | 见第 6 节：这是可选通道，先确认你的设备实现了 `device:tool-config` 监听与执行；未实现就不会响应，属预期行为 |
| 命令行远程点开就报"不支持" | `capabilities` 里是否声明了 `remote_terminal`（见第 9 节）；旧客户端需更新后重连 |
| 画面远程连上就断（公网） | 纯 STUN 打洞失败，需部署 TURN 中继并在管理台填凭据（见 9.4）；命令行远程无此问题 |
| 命令行远程有回显但输入无效 | 确认 `rt:input` 的 `data` 是 base64；收到 `rt:open` 后先回 `rt:data` 再等 `rt:input` |

---

## 13. 一致性承诺（服务器侧行为契约）

服务器对遵循本手册的设备保证：

1. `device:register` 中的 `name` / `deviceType` / `capabilities` / `toolDefs`
   **原样进入** presence 快照，AI 的工具目录、描述、参数 schema 均以设备自报为准；
2. 自定义设备与官方桌面端走**同一条**调度通道（串行队列、超时、重放、结果入库、
   网页实时推送），无功能阉割；
3. 同名工具存在于多台绑定设备时，**优先派发给申报了该工具的设备**；
4. 绑定与授权记录按 `(用户, 设备id)` 持久化，跨重连、跨服务器重启保持；
5. 第 6 节的动态 MCP 定义只由服务器（AI/网页操作）写入，设备重连时会收到
   当前完整集合的补发；设备不实现该通道不影响第 5 节静态 `toolDefs` 的正常调度。
6. 第 9 节的两条远程连接通道（`rc:*` 画面 / `rt:*` 命令行）在**开会话时**统一做
   所有权校验（用户 JWT + 设备属主 + 能力字），与第 8 节任务循环互不干扰；设备
   声明哪个能力就解锁哪条，都不声明就都不开。

协议如有演进，本文件与以下服务端协议注释同步更新，以服务端实现为最终权威：
`connector_runtime/dispatch/device_dispatch.py`（任务分发）、
`connector_runtime/dispatch/remote_control.py`（画面远程 `rc:*`）、
`connector_runtime/dispatch/remote_terminal.py`（命令行远程 `rt:*`）。
