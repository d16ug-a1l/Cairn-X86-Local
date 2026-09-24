# Dispatcher

---

## 本质

Dispatcher 是 Cairn 的客户端执行器。它负责：

1. 协调项目工作区生命周期
2. 给 Agent 下发明确任务
3. 代 Agent 调用 Cairn API 写回图

Agent 不直接认领 Intent，不直接 heartbeat，不直接调用 Cairn API。Agent 只接收 Dispatcher 下发的任务，返回结构化结果；Dispatcher 再决定如何请求 Server。

---

## 设计要点

1. Agent 的输出任务收敛成四类：`bootstrap`、`reason`、`explore` 和 `writeup`；`bootstrap` 只在项目初始态运行，让 Agent 直接尝试解决整个问题；主阶段只有在已解决时才返回，且必须同时给出关键 Fact 和 `complete`；若主阶段超时，再由 `bootstrap_conclude` 收尾产出 Fact；`reason` 负责读图判断是否完成或是否需要提出一个新 intent；`explore` 只负责执行一个已认领 intent 并产出一个 Fact 结论；`writeup` 在项目 `completed` 后运行，基于成功路径事实链和执行记录生成可复现解题过程的 writeup 并写回 Server。
2. Dispatcher 是唯一的协议写入者和控制面；Agent 不 claim、不 heartbeat、不直接调用 Cairn API。
3. 超时策略按任务类型定义；`bootstrap` 和 `explore` 都支持“第一阶段执行 + timeout / parse-fail 后用同一 session 进入 conclude 收尾”的双阶段模式。
4. Prompt 以 markdown 文件形式随代码分发；支持按 prompt 组切换；Worker 行为由 `claudecode`、`codex`、`mock` 等 driver 实现；`dispatch.yaml` 只描述运行期参数。
5. 调度上，项目初始态按 `project.bootstrap_enabled` 和 Worker 能力决定优先 `bootstrap` 或直接 `reason`；非初始态出现新 Fact / Hint 等新态势时优先 `reason`，否则优先消费可认领的 `explore` intent；`reason` 的并发约束通过服务端的项目级 `project.reason` lease 表达为“单项目最多一个”，`bootstrap` 的并发约束是“单项目最多一个保留 bootstrap intent 且最多一个 bootstrap 任务”，跨项目允许并行；`runtime.interval` 被刻意复用为主循环节拍和带 claim 任务的 heartbeat 周期。
6. Worker 按独立的 LLM 并发配额单元建模；同一个 key 不拆成多个 Worker，因此并发控制使用 `workers[].max_running` 即可。
7. 运行日志按“状态变化优先”设计：稳定轮询、正常 heartbeat、重复 skip 原则上不刷屏；工作区创建、任务派发、新进程启动、超时、收尾、释放 intent、worker 进入短暂不可选窗口等事件必须可见。
8. 项目工作区收尾不应阻塞主调度循环；多个已完成项目的工作区 cleanup 可以并行进行。
9. 项目切到非 `active` 后，Dispatcher 必须把它视为硬停止：不再派发新任务；对本地仍在运行的 `bootstrap`、`explore`、`reason` 任务立即发出取消；对已取消任务不再进入 conclude fallback；并在后续轮询中清理该项目工作区、杀掉仍在运行的 Agent 进程。
10. 当前实现按“单 Dispatcher 实例”设计和测试；不支持多个 Dispatcher 同时连接同一服务端共同调度。
11. 若项目曾 `completed` 后又被服务端显式 `reopen` 为 `active`，Dispatcher 不做特殊分支：它会把这视为普通 active 项目继续调度；同时若该项目工作区仍处于已排队 cleanup 状态，Dispatcher 会先等待 cleanup 完成，避免与旧的 completed/stopped cleanup 竞态。
12. 已知限制：当前协议只记录当前 claim 持有者，不保留 Intent 的 worker 历史；因此项目被 `stopped` 后，随着 open intent 的 `worker` 被服务端清空，Dispatcher/UI/API 都无法直接展示“停止前最后是谁在推进这个 intent”。后续若要补这部分可观测性，较合理的方向是在 Intent 上增加类似 `worker_history` 的历史字段，而不是改变当前 claim 语义。

补充：

- 项目被删除后，Dispatcher 会把它视为 `deleted`。这和 `stopped` 一样会先取消本地运行中的任务；项目工作区由 `local.completed_action` 与后续 cleanup 流程按原规则处理，Dispatcher 不再额外维护 orphan 资源。

---

## 架构概览

这个项目在架构上可以分成 4 个部分：

1. Cairn Server
2. Dispatcher
3. 项目工作区
4. Worker / Agent CLI

### 1. Cairn Server

Server 是协议真相源。

它负责：

- 保存 Project / Fact / Intent / Hint
- 提供协议接口
- 维护 Intent 的认领、心跳、结论状态
- 维护项目级 `reason` lease 的认领、心跳与释放状态

### 2. Dispatcher

Dispatcher 是这个工程要实现的核心。

它负责：

- 拉取项目图状态
- 决定当前该派发哪一种任务
- 选择哪个 Worker 来执行
- 管理项目工作区和 Worker 进程
- 维护 session、超时、收尾
- 把结果写回 Cairn Server

### 3. 项目工作区

每个项目对应宿主机上一个独立的工作目录（`<workspace_root>/<project_id>/`，由 `LocalBackend` 管理）。

这个目录是该项目的执行环境，通常负责：

- 作为该项目下所有 Worker 进程的工作目录（cwd）
- 承载探索过程中产出的中间文件、扫描结果等现场数据

项目 `completed` 后，按 `local.completed_action` 决定保留（`keep`，默认）或删除（`remove`）。

### 4. Worker / Agent CLI

Worker 不是协议参与者本身，而是 Dispatcher 管理下的执行单元。

例如：

- Claude Code CLI
- Codex CLI
- Pi CLI

它们直接复用宿主机上已安装、已登录的 CLI，以子进程方式运行在对应项目工作区内。它们负责：

- 接收 Dispatcher 渲染好的 prompt
- 在当前项目工作区内执行任务
- 输出结构化 JSON

### 组件关系

```text
                         +----------------------+
                         |     Cairn Server     |
                         |----------------------|
                         | Projects / Facts     |
                         | Intents / Hints      |
                         | Protocol API         |
                         +----------^-----------+
                                    |
                           read / write API
                                    |
+-----------------------------------------------------------+
|                         Dispatcher                        |
|-----------------------------------------------------------|
| Scheduling / Task Dispatch / Session / Timeout            |
| Workspace Lifecycle / Protocol Writeback                  |
+----------------------+----------------------+-------------+
                       |                      |
              manage workspace        manage workspace
                       |                      |
          +------------v-----------+  +------v-------------+
          |  Project Workspace A   |  | Project Workspace B|
          |  (host subdirectory)   |  | (host subdirectory)|
          |------------------------|  |--------------------|
          | Worker / Agent CLI     |  | Worker / Agent CLI |
          | - Claude Code          |  | - Codex            |
          | - Codex                |  | - ...              |
          +------------------------+  +--------------------+
```

### 执行主链路

Dispatcher 会同时读取两类数据：

- 结构化接口：用于调度、状态判断、intent 选择、协议写回
- `GET /projects/{project_id}/export?format=yaml`：仅用于构造 prompt 所需的图快照

1. Dispatcher 从 Server 读取项目图
2. Dispatcher 依据调度规则选择任务类型和 Worker
3. Dispatcher 渲染 prompt 与命令占位符
4. 如果是 `explore`，Dispatcher 先通过 `POST /projects/{project_id}/intents/{intent_id}/heartbeat` 认领目标 intent；如果是 `reason`，则先通过 `POST /projects/{project_id}/reason/claim` 认领项目级 reason lease
5. Dispatcher 在项目工作区内启动 Worker 子进程
6. Worker 输出结构化 JSON
7. Dispatcher 解析结果，并调用 `POST /projects/{project_id}/complete`、`POST /projects/{project_id}/intents`、`POST /projects/{project_id}/intents/{intent_id}/conclude`、`POST /projects/{project_id}/intents/{intent_id}/release` 或 `POST /projects/{project_id}/reason/release`

项目若被切到 `stopped`，这条主链路会在下一轮短路：Dispatcher 不再把该项目纳入 active 调度集合，会先取消本地仍在运行的任务，再转入工作区 cleanup 流程。对 `bootstrap` / `explore` 来说，被 `stopped` 取消后不会再进入 conclude fallback，因此不会再额外落 Fact。项目恢复为 `active` 后，Dispatcher 会重新读取图状态，再决定是继续 `explore`、进入 `reason`，还是在初始态依据 `project.bootstrap_enabled` 和 Worker 能力重新选择 `bootstrap` 或 `reason`。项目若在 `completed` 后被服务端 `reopen`，对 Dispatcher 来说也等价于“重新变成 active 且图上多了一个新 fact”；下一轮会按普通 active 项目继续调度。

Worker 选择规则：

1. 先按任务类型筛选
2. 再过滤掉已达到 `max_running` 的 Worker
3. 再过滤掉处于本地 `retry_after` 窗口内（最近返回 `accepted: false`）的 Worker
4. 在剩余 Worker 中，优先选择 `priority` 更小的
5. 如果 `priority` 相同，则优先选择当前运行中任务数更少的
6. 如果仍然相同，则随机选择
7. 如果是 `explore`，Dispatcher 先通过 `POST /projects/{project_id}/intents/{intent_id}/heartbeat` claim 成功，再真正启动任务
8. 如果是 `reason`，claim 成功后由 `POST /projects/{project_id}/reason/heartbeat` 维持 lease

---

## 配置模型

Dispatcher 使用一个运行期配置文件：

- `dispatch.yaml`

也就是说：

- 使用者只需要提供 `dispatch.yaml`
- 任务 prompt 以 markdown 文件形式随代码分发，并通过 `runtime.prompt_group` 选择目录
- Worker 的命令模板、session 处理、二阶段收尾能力由对应 driver 实现
- 执行后端固定为 local：Worker 直接在 Dispatcher 宿主机上以子进程运行，复用本机已配置好的 `claude` / `codex` / `pi` CLI（无需 Docker 与 API key）；每个项目分到 `local.workspace_root` 下的独立工作目录

代码目录可以采用类似组织：

```text
dispatcher/
  models.py
  prompting.py
  output_parser.py
  contracts.py
  prompts/
    default/
      bootstrap.md
      bootstrap_conclude.md
      reason.md
      explore.md
      explore_conclude.md
      writeup.md
    mock/
      bootstrap.md
      bootstrap_conclude.md
      reason.md
      explore.md
      explore_conclude.md
      writeup.md
  workers/
    base.py
    registry.py
    adapters/
      claudecode.py
      codex.py
      mock.py
```

本文档附录给出：

- `dispatch.local.example.yaml` 的示例内容
- 上述 markdown prompt 的示例内容

---

## 任务模型

### 四类任务一览

| 任务 | 触发条件 | 输入 | 输出 | 超时策略 |
| --- | --- | --- | --- | --- |
| `bootstrap` | 项目 `active`；`project.bootstrap_enabled=true`，且配置中存在支持 `bootstrap` 的 Worker 或项目已经存在保留 bootstrap intent；facts 只有 `origin` 和 `goal`；当前没有普通 intent；允许不存在 bootstrap intent，或只存在保留的 open `bootstrap` intent | `{origin}`、`{goal}`、`{hints}` | 主阶段成功时固定返回 `fact + complete`；收尾阶段只返回 `fact` | 双阶段：`timeout` 后可进入 `conclude_timeout` 收尾；两阶段都失败则 release 保留 intent，下轮仍按新项目重试 |
| `reason` | 项目 `active`；当前项目无未认领 intent；当前项目内无其他 `reason`；首次触发或满足“新态势”重触发条件 | `{graph_yaml}`、`{fact_ids}`、`{open_intents}` | `complete` 对象；或 `intent` 对象；或空 `data` | 仅 `timeout`；超时或非法结果直接作废，不写图 |
| `explore` | 项目 `active`；存在一个当前可认领的未结论 intent | `{graph_yaml}`、`{intent_id}`、`{intent_description}` | 一个 Fact 结论描述 | 单阶段：超时直接作废；双阶段：超时或输出解析失败时可进入 `conclude` 收尾 |
| `writeup` | 项目 `completed`；`runtime.writeup_enabled=true` 且存在支持 `writeup` 的 Worker；Server 尚无该项目 writeup（`GET` 返回 404）；该项目无运行中任务；本地失败重试窗口已过 | `{project_title}`、`{origin}`、`{goal}`、`{main_chain}`、`{execution_details}` | 一份 Markdown writeup，经 `PUT /projects/{project_id}/writeup` 写回 Server | 仅 `timeout`（`tasks.writeup.timeout`）；单阶段无收尾；失败后进入 30s 本地重试窗口 |

### `bootstrap`

#### 触发条件

- 当前项目仍然是 `active`
- `project.bootstrap_enabled = true`，且配置中存在支持 `bootstrap` 的 Worker 或项目已经存在保留 bootstrap intent
- facts 恰好只有 `origin` 和 `goal`
- intents 为空，或只存在保留的 open `bootstrap` intent
- 保留 `bootstrap` intent 的约定固定为：
  - `description = "bootstrap"`
  - `creator = "dispatcher.bootstrap"`
  - `from = ["origin"]`

#### 输入

- `{origin}`
- `{goal}`
- `{hints}`

其中：

- `{hints}` 是 JSON 数组文本，便于 Worker 在项目起始阶段快速吸收策略信息
- `bootstrap` 不读取图 YAML，不依赖普通 intent 图结构

#### 输出契约

`bootstrap` 使用独立输出契约：

- 主阶段只有在已经解决问题时才返回
- 主阶段返回时必须同时包含 `fact` 和 `complete`
- 如果主阶段没有在超时前解决问题，就不会返回合法结果；Dispatcher 会终止它并进入 `bootstrap_conclude`
- `bootstrap_conclude` 只负责收尾总结，因此只返回 `fact`

```json
{
  "accepted": true,
  "data": {
    "fact": {
      "description": "拿到两个 flag，分别为 flag{...} 与 flag{...}；同时获得管理员 shell，权限证明见 /tmp/proofs/root.txt"
    },
    "complete": {
      "description": "已拿到并验证完成 Goal 所需的全部关键结果，Goal 达成"
    }
  }
}
```

约束：

- 主阶段返回时，`data.fact.description` 和 `data.complete.description` 都必须存在
- 主阶段不允许只返回 `fact`
- `bootstrap_conclude` 只允许返回 `fact`，不允许返回 `complete`

#### 接口映射

`bootstrap` 复用普通 intent 协议，但使用保留 intent：

| 接口 | 用途 | 何时使用 |
| --- | --- | --- |
| `POST /projects/{project_id}/intents` | 创建保留 `bootstrap` intent | 初始态项目首次进入时，如尚不存在该 intent |
| `POST /projects/{project_id}/intents/{intent_id}/heartbeat` | claim 并维持 `bootstrap` intent | 派发前先 claim；执行中按 `interval` 周期发送 |
| `POST /projects/{project_id}/intents/{intent_id}/conclude` | 将 `bootstrap` 产出的关键结果写成 Fact | `bootstrap` 或 `bootstrap_conclude` 返回合法 `fact` 后调用 |
| `POST /projects/{project_id}/complete` | 基于刚写入的 bootstrap fact 直接完成项目 | 仅当 `bootstrap` 主阶段成功返回 `fact + complete` 时调用 |
| `POST /projects/{project_id}/intents/{intent_id}/release` | 放弃本次 bootstrap 尝试 | 两阶段都失败，或命令直接失败时调用 |

#### 超时与失败

- `bootstrap` 第一阶段使用 `timeout`
- `bootstrap_conclude` 第二阶段使用 `conclude_timeout`
- 主阶段如果在 `timeout` 内解决问题，就返回 `fact + complete`
- 第一阶段超时、输出解析失败或返回了不满足契约的结果时，如果 Worker 支持 session / conclude，则进入 `bootstrap_conclude`
- `bootstrap_conclude` 的 prompt 必须明确要求“不继续推进，不等待未完成任务，只总结当前最关键事实”，因此它只产出 `fact`
- 如果 `bootstrap` 主阶段成功返回合法 JSON，则 Dispatcher 会先 conclude 写入 fact，再立即 complete
- 如果 `bootstrap_conclude` 成功返回合法 JSON 且 conclude 写回成功，则保留 intent 被结论落定，项目不再视为初始态
- 如果主阶段的 `complete` 写回失败，已写入的 fact 仍然保留，后续可由下一轮 `reason` 再完成项目
- 如果两阶段都失败，或 conclude 写回失败，则 release 当前 `bootstrap` intent，不写 Fact；项目下轮仍然按新项目处理

### `reason`

#### 触发条件

- 当前项目仍然是 `active`
- 当前项目没有未认领 intent
- 当前项目的 `project.reason` 为空，也就是当前没有其他 `reason` lease 正在运行
- 首次触发只发生在“当前没有任何 open intent”时
- 之后只有出现新的态势才重新触发；这里的“新态势”限定为：
  - Fact 数量增加
  - Hint 数量增加
  - 项目从“存在 open intents”进入“没有 open intents”
- 单次 `explore` 失败、掉心跳、释放但 intent 仍保持 open，不构成新的态势，不应触发新的 `reason`

#### 输入

- `{graph_yaml}`
- `{fact_ids}`
- `{open_intents}`

其中：

- `{fact_ids}` 是 JSON 数组文本，用于显式列出当前合法的 Fact id
- `{open_intents}` 是 JSON 数组文本，用于显式列出当前所有未结论的 intent；因此即使有别的 intent 正在 `explore`，只要出现了新的 Fact / Hint，`reason` 仍可能再次被触发
- 这两个占位符都是 prompt 层辅助，不替代 server 的最终校验

#### 输出契约

已完成：

```json
{
  "accepted": true,
  "data": {
    "complete": {
      "from": ["f008"],
      "description": "flag{abc} 满足 goal 要求"
    }
  }
}
```

未完成，提出新 intent：

```json
{
  "accepted": true,
  "data": {
    "intent": {
      "from": ["f003"],
      "description": "尝试 SQL 注入"
    }
  }
}
```

未完成，不提新 intent：

```json
{
  "accepted": true,
  "data": {}
}
```

约束：

- `data.complete` 存在时，它必须是对象，且 `complete.from` 和 `complete.description` 都必须存在
- `data.complete` 存在时，不应再带 `intent`
- 如果 `intent` 存在，则 `intent.from` 和 `intent.description` 都必须存在
- 如果 `{open_intents}` 为空，说明当前图里没有任何进行中的探索；此时若没有 `data.complete`，则必须返回 `intent`
- 如果 `{open_intents}` 非空，且没有 `data.complete`，则允许不返回 `intent`

#### 接口映射

`reason` 启动前，Dispatcher 必须先 claim 项目级 reason lease，并在执行期间持续 heartbeat；这一状态会直接出现在 `GET /projects` 和 `GET /projects/{project_id}` 的 `project.reason` 字段中，供前端和其他消费者观察。

| `reason` 输出 | Dispatcher 动作 | 备注 |
| --- | --- | --- |
| `data.complete` 存在 | 调用 `POST /projects/{project_id}/complete` | `worker` 使用当前执行该任务的 `workers[].name` |
| `data.complete` 不存在，且带 `intent` | 调用 `POST /projects/{project_id}/intents` | `creator` 使用当前 Worker 名；`worker` 固定写 `null` |
| `data` 为空对象 | 不写图 | 不写 Fact / Intent / Complete |

写回失败的日志语义：

- 如果 `POST /projects/{project_id}/complete` 或 `POST /projects/{project_id}/intents` 返回 `403`，通常表示项目已不再是 `active`，本次任务直接作废，记 `info`
- 其他写入失败也直接作废，不做立即重试，只记日志
- 无论本轮是否写图，只要项目仍是 `active` 且 reason lease 仍在自己手里，Dispatcher 都会在收尾时调用 `POST /projects/{project_id}/reason/release`；如果项目已 `completed` 或 `stopped`，则由服务端直接清空该 lease

#### 超时与失败

- `reason` 只使用 `timeout`
- 超时直接作废
- `accepted: false` 直接作废，记 `warn`
- 其他执行错误也直接作废，例如：
  - 命令退出码非 `0`
  - 输出不是合法 JSON
  - JSON 缺少必要字段
- 以上情况都不写 Fact / Intent / Complete，只记日志

### `explore`

#### 触发条件

- 当前项目仍然是 `active`
- 存在一个当前可认领的、尚无结论的 intent

#### 输入

- `{graph_yaml}`
- `{intent_id}`
- `{intent_description}`

#### 输出契约

正常返回：

```json
{
  "accepted": true,
  "data": {
    "description": "发现 /search 参数存在报错注入"
  }
}
```

即使没有打出漏洞，也应返回一个客观探索结论，而不是空响应。例如：

```json
{
  "accepted": true,
  "data": {
    "description": "对 /search 参数测试常见 SQL 注入 payload，未发现可利用注入迹象"
  }
}
```

约束：

- `data.description` 必须存在，且必须是客观事实结论
- 不允许输出“我拒绝帮助渗透”“这不安全”等文本作为 `description`

#### 接口映射

`explore` 会涉及三类协议接口：

| 接口 | 用途 | 何时使用 |
| --- | --- | --- |
| `POST /projects/{project_id}/intents/{intent_id}/heartbeat` | claim 并维持持有 | 派发前先 claim；执行中按 `interval` 周期发送 |
| `POST /projects/{project_id}/intents/{intent_id}/conclude` | 产出 Fact 并结论落定 Intent | `execute` 或 `conclude` 返回合法结论后调用 |
| `POST /projects/{project_id}/intents/{intent_id}/release` | 放弃当前尝试 | 失败路径使用 |

派发顺序要求固定为：

1. Dispatcher 先选中一个可认领 intent
2. Dispatcher 先调用一次 `POST /projects/{project_id}/intents/{intent_id}/heartbeat` 作为 claim
3. 只有 heartbeat 成功后，才真正启动 `explore` 对应的 Worker

#### 超时与失败

`explore` 要兼容两种模式：

1. 单阶段模式：只有 `execute`
2. 双阶段模式：`execute + session + conclude`

其中 `conclude` 是附加收尾阶段，不是主流程必经阶段。

第一阶段使用 `timeout`。
如果是双阶段模式，第二阶段使用 `conclude_timeout`。

正常完成：

- 如果 `execute` 在 `timeout` 内正常返回合法 JSON，且 `accepted: true`
- Dispatcher 直接调用 `POST /projects/{project_id}/intents/{intent_id}/conclude`
- `POST /projects/{project_id}/intents/{intent_id}/conclude` 成功即完成结论落定，无需额外 `release`
- 如果 `POST /projects/{project_id}/intents/{intent_id}/conclude` 写入失败，本次任务直接作废，不做立即重试，释放当前 intent，只记日志

可进入二阶段收尾的异常：

- 这类异常只在“双阶段 Worker”上进入 `conclude`
- 适用异常只有两种：
  - 执行超时
  - Dispatcher 无法从第一阶段输出里正确提取并解析结果，例如：
    - 输出不是合法 JSON
    - JSON 缺少必要字段
    - `accepted: true` 但 `data` 结构不符合当前任务要求
- 这类异常在“单阶段 Worker”上不进入 `conclude`

双阶段收尾流程固定为：

1. Dispatcher 杀掉当前进程
2. 保留这次任务对应的 session id
3. 在保持 heartbeat 的前提下，用同一个 session 直接进入 `conclude`
4. `conclude` 的 prompt 必须明确要求“不要继续探索，只总结截至目前已经完成的探索与结论”
5. 如果 `conclude` 在 `conclude_timeout` 内返回合法 JSON，且 `accepted: true`：
   - Dispatcher 调用 `POST /projects/{project_id}/intents/{intent_id}/conclude`
   - 成功则结束
6. 如果 `conclude` 再次超时，或输出不合法，或返回 `accepted: false`，或 `POST /projects/{project_id}/intents/{intent_id}/conclude` 写入失败：
   - 整次探索作废
   - 不写任何图数据
   - 释放当前 intent
   - 只记日志

单阶段 Worker 的异常处理：

- 如果当前 Worker 不支持 `session` 或 `conclude`，则它属于单阶段模式
- 这时第一阶段一旦出现“超时”或“输出解析 / 结构校验失败”，直接按失败处理
- 处理方式是：杀进程、整次探索作废、不写任何图数据、释放当前 intent、记 `warn`

直接失败，不进入 `conclude`：

- 第一阶段返回 `accepted: false`
- 命令退出码非 `0`
- Worker 进程根本没有产生可读取结果
- Dispatcher 在进入结果解析前就已经确定本次执行失败

以上情况都：

- 不进入 `conclude`
- 不写任何图数据
- 释放当前 intent
- 清理本地任务状态
- 只记日志

### `writeup`

`writeup` 是项目完成后的派生任务：为已 `completed` 的项目生成一份可复现解题过程的中文 writeup，并写回 Server 存储。它不修改事实图，不属于探索写操作。

#### 触发条件

- 项目状态为 `completed`
- `runtime.writeup_enabled = true`，且配置中存在 `task_types` 包含 `writeup` 的 Worker；否则整个 writeup 功能关闭（旧配置自然不含 `writeup`，即默认关闭调度）
- Server 尚无该项目 writeup：`GET /projects/{project_id}/writeup` 返回 `404`。若已存在（`200`），记入本地完成集合并跳过——重启后靠这个 GET 判重，不依赖持久化的本地状态。已进入完成集合的项目也会每 60s（`WRITEUP_VERIFY_INTERVAL_SECONDS`）重新核对一次：若 writeup 已被删除（如 UI 的「重新生成」），自动重新调度生成；核对请求失败时保持原状态，不误触发重生
- 该项目当前没有运行中任务，且项目工作区不在 cleanup 队列中
- 本地失败重试窗口已过（见下方）

#### 输入

- `{project_title}`、`{origin}`、`{goal}`
- `{main_chain}`：成功路径事实链。从 `origin` 出发沿已结论 intent 做 BFS，取到达 `goal` 的链；`goal` 不可达时退化为当前最深链。按步骤渲染每一步的描述、执行者、依据 fact、时间与产出 fact
- `{execution_details}`：每一步对应的执行记录（操作叙述 + 实际执行的命令）。从 claude transcript 提取（复用报告生成的 transcript 解析，`text_limit=1000`、`command_limit=2000`）；transcript 缺失或收集失败时降级为仅依据事实链重建步骤的提示文本

#### 执行模型

- 单阶段：只有 `execute`，没有 conclude fallback
- 不 claim intent，不使用心跳租约；取消只通过本地 `TaskCancellation` 传递

#### 输出契约

```json
{
  "accepted": true,
  "data": {
    "writeup": "# xx渗透测试 Writeup\n\n## 概述\n..."
  }
}
```

约束：

- `data.writeup` 必须存在，且是非空 Markdown 字符串
- 校验由 `contracts.validate_writeup_payload` 完成；`accepted: false`、JSON 非法或字段缺失都按失败处理

#### 接口映射与失败处理

| 情况 | Dispatcher 动作 |
| --- | --- |
| 合法 `writeup` | `PUT /projects/{project_id}/writeup`，`worker` 使用当前 Worker 名 |
| `PUT` 返回 `409` | 项目已不再是 `completed`（如被 `reopen`），本次结果丢弃，按取消处理 |
| `PUT` 其他失败 / 超时 / 解析失败 / `accepted: false` | 不写回，只记日志，并给该项目设置 30s 本地重试窗口（`WRITEUP_RETRY_AFTER_SECONDS`），窗口过后下轮重新触发 |
| 写回成功 | 记入本地 `_writeup_done` 集合，不再重复调度 |

#### 取消语义

- `writeup` 任务运行在 `completed` 项目上，因此 `_cancel_inactive_tasks` 不会仅因项目 `completed` 而取消它
- 项目被 `reopen`（回到 `active`）、`stopped` 或删除时，运行中的 `writeup` 任务会被取消

---

## 调度策略

### 全局调度

核心规则：

1. 已运行项目优先，但只优先可立即派发的任务
2. 如果某个运行中项目处于初始态且可执行 `bootstrap`，优先继续它
3. 否则如果某个运行中项目存在可执行的 `explore`，优先继续探索它
4. 如果所有运行中项目都暂时没有可派发任务，且 `runtime.max_running_projects` 还有余量，就启动一个未开始的新项目

`runtime.interval` 的设计约定：

- `runtime.interval` 不只是一个普通轮询间隔
- 它被刻意复用为两个地方的统一节拍：
  - Dispatcher 主循环间隔
  - 带 claim 任务（`bootstrap` / `explore`）的 heartbeat 周期
- 这样做的目标是减少额外时序参数，先保持实现简单
- 这是一项明确设计决策，不是偶然耦合

调度伪代码可以保持成下面这种粒度：

```text
for project in running_projects_round_robin:
  if has_dispatchable_bootstrap(project):
    dispatch_bootstrap(project)
    continue
  if has_dispatchable_reason(project):
    dispatch_reason(project)
    continue
  if has_dispatchable_explore(project):
    dispatch_explore(project)
    continue

if running_project_count < runtime.max_running_projects:
  maybe_start_one_new_project()
```

### 项目内调度

对于单个项目，Dispatcher 读完整项目状态后，按下面顺序调度：

1. 如果项目仍处于初始态，先按 `project.bootstrap_enabled` 和 Worker 能力决定路径：未开启或没有支持 `bootstrap` 的 Worker 时直接 `reason`，否则执行 `bootstrap`；若已经存在保留 bootstrap intent，则继续该阶段
2. 如果满足“新态势”重触发条件，优先派发 `reason`
3. 否则如果存在未认领 intent，派发 `explore`
4. `reason` 的去重按“态势”做，而不是按总图变化做：首次只有在当前没有任何 open intent 时才触发；之后只有当前 Fact / Hint 数量增加，或项目从“存在 open intents”进入“没有 open intents”时，才重新触发
5. 如果 `reason` 返回 `data.complete`，Dispatcher 调用 `POST /projects/{project_id}/complete`
6. 如果 `reason` 没有返回 `data.complete` 且带 `intent`，Dispatcher 调用 `POST /projects/{project_id}/intents`
7. 如果 `reason` 既没有返回 `data.complete`，也没有返回 `intent`，则本轮不写图

另外：

- 初始态项目里，如果选择了 `bootstrap` 路径且 bootstrap intent 已被 claim，则这一轮不再派发 `reason` 或普通 `explore`
- 即使同一项目里已经有进行中的 `explore`，也允许继续派发一个 `reason` 任务
- 但前提不是“刚新增了 intent”，而是“确实出现了新的 Fact / Hint 等新态势”；仅仅因为上一个 `reason` 刚创建了新的 intent，不应该立刻再次 `reason`
- 仍然要求当前没有未认领 intent、当前项目内没有其他 `reason` 任务在运行、且没有超过 `runtime.max_project_workers`

`reason` 的去重规则建议保持简单：

- 记录该项目上次成功完成 `reason` 时的 Fact 数量、Hint 数量，以及当时是否仍存在 open intents
- Dispatcher 对“当前已观察到、且已有 open intents、但尚无 checkpoint 的 active 项目”建立基线 checkpoint；启动时已有项目和运行中晚到项目都适用，不应吞掉运行过程中第一次新增的 Fact / Hint
- 首次没有历史记录且当前没有 open intents 时，直接触发
- 之后只有 Fact / Hint 数量增加，或项目从“有 open intents”变为“无 open intents”时，才再次触发 `reason`
- 不把“总 intent 数量增加”当作重触发条件；因为这通常只是上一次 `reason` 刚创建了新 intent，并不代表出现了新的态势
- `explore` 的执行失败、掉心跳、临时 release 只会让 intent 重新等待被探索，不会额外触发 `reason`

### `writeup` 调度与清理门控

`writeup` 不参与 active 项目的调度循环。主循环每 tick 的顺序是：

1. `_cancel_inactive_tasks`：取消非活跃项目上的本地任务；运行在 `completed` 项目上的 `writeup` 任务不在取消之列（`reopen` / `stopped` / 删除仍会取消它）
2. `_dispatch_writeups`：扫描所有 `completed` 项目，对满足触发条件的项目派发 `writeup`（受 `runtime.max_workers` 全局并发约束）
3. `_queue_workspace_cleanups`：对 completed/stopped 项目排队工作区清理
4. `_dispatch_available`：active 项目的正常调度（`bootstrap` / `reason` / `explore`）

清理门控：当 `runtime.writeup_enabled=true` 且存在支持 `writeup` 的 Worker 时，`completed` 项目的工作区清理会等 writeup 生成完成（进入 `_writeup_done`）后才排队，保证 writeup 任务仍能在项目工作区内运行、且 transcript 仍可读取。writeup 功能关闭（未启用或无 Worker 支持）时不存在该门控，清理按原逻辑进行。

### 并发约束

- 当前设计下，只支持一个 Dispatcher 实例连接同一服务端执行调度
- 如果同时运行多个 Dispatcher，本地维护的 admission、并发计数、工作区清理和 bootstrap 去重都不会跨进程协调，因此不属于支持场景
- 单个项目内，同一时刻最多只能有一个 `bootstrap` 任务在运行
- Dispatcher 会尽力让单个项目在初始态时只保留一个 open `bootstrap` intent
- 单个项目内，同一时刻最多只能有一个 `reason` 任务在运行
- 跨项目允许并行运行多个 `reason`
- `reason` 也计入对应项目的 `runtime.max_project_workers`
- `runtime.max_workers`：Dispatcher 同时运行中的任务总数上限
- `runtime.max_running_projects`：当前 dispatcher 运行期内已接手且仍为 `active` 的项目 admission 上限；项目即使暂时没有可派发任务，只要仍为 `active`，也继续占用该名额，直到其退出 active
- `runtime.max_project_workers`：单个项目内同时运行的任务上限，统一计入 `bootstrap`、`reason` 和 `explore`
- `workers[].max_running`：单个 Worker 自身的并发上限；达到上限后，这个 Worker 暂时不再参与派发

---

## Worker 配置

### 字段定义

Dispatcher 固定从 `stdout` 取全文作为模型正文输出。

`dispatch.yaml` 中与 Worker 相关的运行期字段如下：

| 字段 | 含义 | 说明 |
| --- | --- | --- |
| `name` | Worker 静态标识 | 协议写回时作为 `creator` 或 `worker` |
| `type` | Worker driver 名 | 支持 `claudecode`、`codex`、`pi`、`mock` |
| `task_types` | 支持的任务类型 | `bootstrap`、`reason`、`explore`、`writeup`；只有显式包含 `writeup` 的 Worker 才会被调度 writeup 任务 |
| `max_running` | Worker 并发上限 | 达到上限后暂不派发 |
| `priority` | 选择优先级 | 数字越小越优先 |
| `env` | 运行时环境变量 | 叠加在宿主机环境之上传给 Worker 子进程；`mock` 的 phase 耗时和结果概率也通过这里配置 |

系统提供四类 Worker driver：

- `claudecode`
- `codex`
- `pi`
- `mock`

也就是说：

- `dispatch.yaml` 负责声明“用哪个 driver、能跑什么任务、并发多少、环境变量是什么”；如果是 `mock`，各 phase 的模拟分布也放在 `env`
- 具体怎么启动命令、怎么提取 session、怎么恢复 `conclude`，都由 driver 代码负责

### Driver 接口

每个 Agent / CLI 工具在代码里对应一个独立 driver 文件，并实现统一接口。driver 注册在单一 `DRIVERS` 字典中，通过 `get_driver(name)` 获取。

统一接口至少应覆盖这些能力：

- `local_binary()`：该 driver 在宿主机上调用的可执行文件名（如 `claude`、`codex`、`pi`），启动时用于 PATH 检查；`mock` 返回 `None`
- `prepare_session()`：需要时预先生成 session id
- `build_execute(worker, prompt, session)`：构造第一阶段执行命令
- `extract_session(session, stdout, stderr)`：需要时从输出中提取 session id，或继续使用预生成 session
- `build_conclude(worker, prompt, session)`：在双阶段 `explore` 中恢复同一 session 做收尾
- `supports_conclude()`：声明该 driver 是否支持双阶段 `explore`

这些 driver 的能力约定是：

- `claudecode` 支持双阶段 `explore`
- `codex` 支持双阶段 `explore`
- `pi` 支持双阶段 `explore`
- `mock` 支持双阶段 `explore`

并发建模约定：

- 一个 Worker 应代表一个独立的 LLM 并发配额单元
- 不考虑“多个 Worker 共用同一个账号 / 配额”的情况
- 并发控制使用 `workers[].max_running`

### 启动时 CLI 检查

不再有任何针对 LLM API 的健康检查（没有 `check_health`、没有任务前探活、没有 unhealthy 重试窗口）。唯一的启动检查是本地 CLI 二进制检查（`loop.py` 的 `_run_local_binary_check`）：

- 启动时对每个已配置 Worker 对应 driver 的 `local_binary()` 执行 `shutil.which` + `--help` 探测
- 全部缺失时报错退出；部分缺失时只告警，对应 Worker 无法运行
- 同时提醒各 CLI 必须已在宿主机登录、可直接使用——Cairn 不注入任何 API key
- `cairn dispatch --startup-healthcheck-only` 执行的就是这个本地 CLI 检查

### CLI 接入约定

Worker 进程直接以宿主机子进程方式启动，cwd 为项目工作区，环境为 `{**os.environ, **worker.env}`。所有 CLI 都使用其宿主机自身配置（登录态、模型、provider），Dispatcher 不注入任何 LLM 环境变量。

#### `claudecode` driver

已知行为：

- driver 预先生成 session id
- 首轮可以预先指定 session id
- 如果该 id 已存在，命令会报错，不会复用旧会话
- Dispatcher 固定从 `stdout` 取全文作为结果正文

第一阶段执行：

```bash
claude --session-id "{session}" --dangerously-skip-permissions -p -- "{prompt}"
```

二阶段收尾：

```bash
claude -r "{session}" --dangerously-skip-permissions -p -- "{prompt}"
```

#### `codex` driver

已知行为：

- 首轮 session id 会打印在 `stderr`
- 可以用正则 `session id:\s*([0-9a-fA-F-]+)` 提取
- Dispatcher 固定从 `stdout` 取全文作为结果正文

第一阶段执行：

```bash
codex exec --dangerously-bypass-approvals-and-sandbox -- "{prompt}"
```

二阶段收尾：

```bash
codex exec resume "{session}" --dangerously-bypass-approvals-and-sandbox -- "{prompt}"
```

#### `pi` driver

已知行为：

- 不注入 models.json，也不传 `--provider` / `--model`，pi 完全使用宿主机自身配置
- 通过一个很小的 sh wrapper 确保 session 目录存在后再 exec `pi`
- session id 从 `stdout` 的 JSON 事件流（`type == "session"`）中提取
- 模型正文从 `stdout` 的 JSON 事件流中取最后一条 assistant 消息

执行命令（`--session "{session}"` 仅在已有 session 时追加）：

```bash
pi --mode json --session-dir "{session_dir}" \
  --no-extensions --no-skills --no-prompt-templates --no-themes --no-context-files \
  --tools read,write,edit,bash,grep,find,ls \
  -p "{prompt}"
```

#### `mock` driver

`mock` driver 用于本地观察 dispatcher 的成功、失败和超时路径。

行为约定：

- `runtime.prompt_group: "mock"` 时，prompt 本身是结构化 JSON，不再依赖自然语言说明
- `reason` prompt 最少包含 `phase`、`fact_ids`、`open_intents`
- `explore` / `conclude` prompt 最少包含 `phase`、`intent_id`
- `bootstrap` / `bootstrap_conclude` prompt 最少包含 `phase`、`origin`、`goal`、`hints`
- `writeup` prompt 最少包含 `phase`、`origin`、`goal`
- driver 会先解析 prompt 里的 `phase` 字段，再读取对应的 `MOCK_<PHASE>` JSON 环境变量选择当前 phase 的模拟结果
- 每个 phase 都在自己的 JSON 里配置 `delay: [min, max]`；单位是秒，支持小数；随机耗时超过 Dispatcher 外层 timeout 时，就会自然表现为超时
- `reason.noop` 只会在 `open_intents` 非空时参与抽样；mock 会自动避开当前上下文下不合法的结果

`mock` 支持六个 phase：

- `bootstrap`
- `bootstrap_conclude`
- `reason`
- `explore_execute`
- `explore_conclude`
- `writeup`

支持的结果如下：

- `bootstrap.outcomes`: `complete`、`fact`、`rejected`、`invalid_json`、`invalid_payload`、`command_fail`
- `bootstrap_conclude.outcomes`: `fact`、`rejected`、`invalid_json`、`invalid_payload`、`command_fail`
- `reason.outcomes`: `complete`、`intent`、`noop`、`rejected`、`invalid_json`、`invalid_payload`、`command_fail`
- `explore_execute.outcomes`: `fact`、`rejected`、`invalid_json`、`invalid_payload`、`command_fail`
- `explore_conclude.outcomes`: `fact`、`rejected`、`invalid_json`、`invalid_payload`、`command_fail`
- `writeup.outcomes`: `writeup`、`rejected`、`invalid_json`、`invalid_payload`、`command_fail`

命名约定：

- 每个 phase 一个变量：`MOCK_<PHASE>`，例如 `MOCK_REASON`；不存在 `MOCK_HEALTHCHECK`（已随健康检查机器一并移除，配置它会被拒绝为 unsupported mock env keys）
- 变量值必须是 JSON 对象，结构为：`{"delay":[min,max],"outcomes":{...}}`
- `delay` 必须是两个非负数字，单位是秒
- 每个 phase 的所有结果概率都使用 `0` 到 `1` 的小数，并且总和必须严格等于 `1.0`

### 配置校验规则

#### 加载时校验

启动 Dispatcher 时，应完成静态校验：

- `dispatch.yaml` 必须存在且可读取
- `runtime.max_workers` 必须存在
- `runtime.max_running_projects` 必须存在
- `runtime.max_project_workers` 必须存在
- `runtime.interval` 必须存在
- `runtime.prompt_group` 必须存在
- `runtime.writeup_enabled` 如果存在，必须是布尔值；缺省为 `true`
- 旧配置中遗留的 `runtime.execution`、`runtime.worker_healthcheck`、`runtime.healthcheck_timeout` 会被静默忽略；但顶层 `container:` 节会被拒绝（`extra=forbid`）
- `tasks.bootstrap.timeout` 必须存在
- `tasks.bootstrap.conclude_timeout` 必须存在
- `tasks.reason.timeout` 必须存在
- `tasks.explore.timeout` 必须存在
- `tasks.explore.conclude_timeout` 必须存在
- `tasks.writeup.timeout` 可选；缺省为 `900` 秒，旧配置不写也能加载
- 每个 Worker 都必须有 `type`
- 每个 Worker 都必须有 `max_running`
- `task_types` 只允许 `bootstrap`、`reason`、`explore`、`writeup`
- `type` 只允许 `claudecode`、`codex`、`pi`、`mock`
- `max_running` 必须是正整数
- Worker 的 `env` 不再做按 driver 的强制 key 校验（本机 CLI 自带配置）；仅 `mock` 校验 `MOCK_*` 变量
- `claudecode`、`codex`、`pi`、`mock` 都支持双阶段 `explore`
- `runtime.prompt_group` 对应的 prompt 目录必须存在
- 代码工程中的 prompt 资源必须存在
- 默认 prompt 组下，`reason.md` 必须至少覆盖 `{graph_yaml}`、`{fact_ids}`、`{open_intents}`
- 默认 prompt 组下，`explore.md` 必须至少覆盖 `{graph_yaml}`、`{intent_id}`、`{intent_description}`
- 默认 prompt 组下，`bootstrap.md` 和 `bootstrap_conclude.md` 必须至少覆盖 `{origin}`、`{goal}`、`{hints}`
- 默认 prompt 组下，`writeup.md` 必须至少覆盖 `{project_title}`、`{origin}`、`{goal}`、`{main_chain}`、`{execution_details}`
- `mock` prompt 组下，`reason.md` 必须至少覆盖 `{fact_ids}`、`{open_intents}`
- `mock` prompt 组下，`explore.md` 和 `explore_conclude.md` 必须至少覆盖 `{intent_id}`
- `mock` prompt 组下，`bootstrap.md` 和 `bootstrap_conclude.md` 必须至少覆盖 `{origin}`、`{goal}`、`{hints}`
- `mock` prompt 组下，`writeup.md` 必须至少覆盖 `{origin}`、`{goal}`
- `mock` worker 的 `MOCK_*` 变量名只能使用系统支持的 phase（含 `MOCK_WRITEUP`，不含 `MOCK_HEALTHCHECK`）
- `mock` worker 每个 phase 的概率都必须在 `0` 到 `1` 之间，且总和必须严格等于 `1.0`

#### 运行时校验

任务真正派发时，还需要做运行时校验：

- driver 必须存在且支持该任务类型
- 只有支持当前任务类型的 Worker 才能被选中
- 只有当前运行中任务数小于 `max_running` 的 Worker 才能被选中
- 处于本地 `retry_after` 窗口内（最近返回 `accepted: false`）的 Worker 不参与派发
- `bootstrap` 和 `explore` 都必须先完成 claim，成功后才真正启动任务线程
- 如果要走双阶段 `explore`，则第一阶段必须成功拿到 session id，才能进入 `conclude`
- 如果要走双阶段 `bootstrap`，则第一阶段必须成功拿到 session id，才能进入 `bootstrap_conclude`
- Dispatcher 必须对 `stdout` 全文做 JSON 解析和任务级结构校验
- `accepted: false`、JSON 非法、字段缺失、接口写回失败等情况都必须记日志，且不做立即重试

---

## 配置字段速查

### `dispatch.yaml`

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `server` | 是 | Cairn Server 的 base URL |
| `active_worker` | 否 | 当前启用的 worker 名；设置后只有该 worker 参与新任务派发。Dispatcher 每轮检查配置文件 mtime 并热加载（切换数秒内生效，仅影响新任务；`server` 地址变更仍需重启）。也可通过 Server 的 `GET /llm/workers` 与 `PUT /llm/active` 接口及前端弹窗切换 |

### `runtime.*`

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `runtime.max_workers` | 是 | Dispatcher 同时运行中的任务总数上限 |
| `runtime.max_running_projects` | 是 | 当前 dispatcher 运行期内已接手且仍为 `active` 的项目上限 |
| `runtime.max_project_workers` | 是 | 单个项目内同时运行的任务上限，统一计入 `bootstrap`、`reason` 和 `explore` |
| `runtime.interval` | 是 | 统一节拍配置；既是 Dispatcher 主循环间隔，也是带 claim 任务的 heartbeat 周期 |
| `runtime.prompt_group` | 是 | 当前使用的 prompt 组目录名 |
| `runtime.writeup_enabled` | 否 | 是否在项目 `completed` 后自动生成 writeup；默认 `true`。开启后还需至少一个 Worker 的 `task_types` 显式包含 `writeup`，writeup 任务才会被调度 |

旧配置中遗留的 `runtime.execution`、`runtime.worker_healthcheck`、`runtime.healthcheck_timeout` 会被静默忽略；顶层 `container:` 节会被拒绝。

### `local.*`

可选。此时 worker 不需要任何 LLM 环境变量；启动时 Dispatcher 会对每个已配置 worker 的 CLI 执行 PATH + `--help` 探测，全部缺失则报错退出。

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `local.workspace_root` | 否 | 每项目工作目录的根；不填则取 dispatcher 启动时的当前目录，每项目分到隔离子目录 `<root>/<project_id>/` 作为 worker 进程的工作目录 |
| `local.completed_action` | 否 | 项目 completed 后对工作目录的处理：`keep`（默认，保留现场）或 `remove` |

### `tasks.*`

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `tasks.bootstrap.timeout` | 是 | `bootstrap` 第一阶段超时 |
| `tasks.bootstrap.conclude_timeout` | 是 | `bootstrap` 双阶段收尾超时 |
| `tasks.reason.timeout` | 是 | `reason` 的超时 |
| `tasks.explore.timeout` | 是 | `explore` 第一阶段超时 |
| `tasks.explore.conclude_timeout` | 是 | `explore` 双阶段收尾超时 |
| `tasks.writeup.timeout` | 否 | `writeup` 任务的超时；缺省 `900` 秒 |

### `workers.*`

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `name` | 是 | Worker 静态标识；协议写回时使用这个值作为 `creator` 或 `worker` |
| `type` | 是 | Worker driver 名；支持 `claudecode`、`codex`、`pi`、`mock` |
| `task_types` | 是 | 该 Worker 支持的任务类型列表 |
| `max_running` | 是 | 该 Worker 自身的并发上限 |
| `priority` | 是 | 当前任务类型的候选 Worker 中，数字越小优先级越高 |
| `env` | 是 | 该 Worker 的变量表；叠加在宿主机环境之上，仅 `mock` 的 `MOCK_*` 变量在启动时校验 |

补充：

- Worker 选择顺序是：先过滤任务类型、`max_running` 和处于本地 `retry_after` 窗口内的 Worker，再按 `priority`，同优先级优先选当前运行数更少的，最后随机；`bootstrap` 和 `explore` 都会先 claim，再启动任务
- 执行命令、session 提取、二阶段 `conclude` 都由对应 driver 代码负责
- prompt 内容从代码工程里的 markdown 资源加载

---

## 附录：示例配置与 Prompt 内容

### `dispatch.local.example.yaml`

```yaml
server: "http://127.0.0.1:8000"

runtime:
  max_workers: 3  # total running tasks; all workers share this host — size to the machine
  max_running_projects: 2  # total active projects admitted by this dispatcher runtime
  max_project_workers: 2  # per-project running tasks, including bootstrap + reason + explore
  interval: 3  # intentional shared cadence: scheduler loop interval + claim-task heartbeat interval, in seconds
  prompt_group: "default"  # selects prompts/<group>/
  writeup_enabled: true  # auto-generate a writeup once a project is completed (also needs a worker with task_types including writeup)

tasks:
  bootstrap:
    timeout: 120
    conclude_timeout: 30
  reason:
    timeout: 45
  explore:
    timeout: 600
    conclude_timeout: 120
  writeup:
    timeout: 900

# Optional. If omitted: workspace_root defaults to the dispatcher's current directory,
# completed_action defaults to keep. Each project gets an isolated <workspace_root>/<project_id>/.
local:
  # workspace_root: "/data/cairn-runs"
  completed_action: keep  # keep | remove

# Extra environment variables for every local worker process, merged over the dispatcher's
# own environment ({**os.environ, **common_env}). Per-worker `env:` overrides common_env.
# common_env:
#   https_proxy: "http://127.0.0.1:7897"

# No API keys needed — the host CLIs are used as-is, with their own host configuration
# and credentials. `env:` is only needed by the mock driver (MOCK_* variables).
workers:
  - name: "local-claude"
    type: "claudecode"
    task_types: [bootstrap, reason, explore, writeup]
    max_running: 1
    priority: 0  # lower number wins; ties prefer fewer running tasks, then choose randomly

  # - name: "local-codex"
  #   type: "codex"
  #   task_types: [explore]
  #   max_running: 1
  #   priority: 1

  # - name: "local-pi"
  #   type: "pi"
  #   task_types: [reason]
  #   max_running: 1
  #   priority: 2

  - name: "mock-observer"
    type: "mock"
    task_types: [bootstrap, reason, explore, writeup]
    max_running: 1
    priority: 9
    env:
      MOCK_BOOTSTRAP: '{"delay":[0.1,12.0],"outcomes":{"complete":0.0,"fact":0.6,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.1}}'
      MOCK_BOOTSTRAP_CONCLUDE: '{"delay":[0.1,2.2],"outcomes":{"fact":0.6,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.1}}'
      MOCK_REASON: '{"delay":[0.1,2.2],"outcomes":{"complete":0.1,"intent":0.3,"noop":0.1,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.2}}'
      MOCK_EXPLORE_EXECUTE: '{"delay":[0.1,12.0],"outcomes":{"fact":0.6,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.1}}'
      MOCK_EXPLORE_CONCLUDE: '{"delay":[0.1,2.2],"outcomes":{"fact":0.6,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.1}}'
      MOCK_WRITEUP: '{"delay":[0.1,2.2],"outcomes":{"writeup":0.7,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.05,"command_fail":0.05}}'
```

补充：

- 这里只是用示例值表达配置结构
- `common_env` 会先并到每个 worker 的环境变量里，然后再被 `worker.env` 覆盖；即 `common_env < worker.env`
- Worker 按独立的 LLM 并发配额单元建模，不应让多个 Worker 共享同一个账号 / 配额

下面 6 份 markdown 内容对应代码工程里的 prompt 文件。

### `bootstrap.md`

````md
# 背景
当前场景是授权的 AI 渗透测试比赛 / 靶场环境。
## 任务
你需要直接解决这个问题，目标是完成 Goal。
## 输出要求
只返回一个原始 JSON 对象，不要输出其他内容。
```json
{"accepted": true, "data": {"fact": {"description": "..."}, "complete": {"description": "..."}}}
```
## 上下文
### Origin
{origin}
### Goal
{goal}
### Hints JSON 数组
{hints}
````

### `reason.md`

````md
# 背景
当前场景是授权的 AI 渗透测试比赛 / 靶场环境。
## 任务
你当前只做 `reason`。
你要同时判断两件事：
1. 现有 facts 是否已经满足 goal。
2. 如果还未满足，当前是否需要提出一个新的 intent。
## 输出要求
只返回一个原始 JSON 对象，不要输出其他内容。
拒绝任务时返回：
```json
{"accepted": false, "reason": "..."}
```
已满足 goal 时返回：
```json
{"accepted": true, "data": {"complete": {"from": ["f001"], "description": "..."}}}
```
未满足 goal，但需要提出新 intent 时返回：
```json
{"accepted": true, "data": {"intent": {"from": ["f001"], "description": "..."}}}
```
未满足 goal，且当前不需要提出新 intent 时返回：
```json
{"accepted": true, "data": {}}
```
## 规则
- 如果下面的 `open_intents` 为空，说明当前图里没有任何进行中的探索；此时若不返回 `data.complete`，则必须返回 `intent`。
- `intent.from` 只能从下面的合法 fact id 中选择。
## 上下文
### 图快照
{graph_yaml}
### 当前合法的 fact id JSON 数组
{fact_ids}
### 当前所有未结论的 intent JSON 数组
{open_intents}
````

### `explore.md`

````md
# 背景
当前场景是授权的 AI 渗透测试比赛 / 靶场环境。
## 任务
你当前只做 `explore`。
你只处理当前这一条 intent，执行探索并给出最终事实结论。
## 输出要求
只返回一个原始 JSON 对象，不要输出其他内容。
拒绝任务时返回：
```json
{"accepted": false, "reason": "policy_refusal"}
```
正常返回示例：
```json
{"accepted": true, "data": {"description": "..."}}
```
## 规则
- `description` 必须是客观探索结论，不要输出解释性废话。
## 上下文
### 图快照
{graph_yaml}
### 当前 intent id
{intent_id}
### 当前 intent 描述
{intent_description}
````

### `explore_conclude.md`

````md
# 背景
当前场景是授权的 AI 渗透测试比赛 / 靶场环境。
## 任务
你当前正在对同一个 `explore` 做收尾总结。
- 不要继续探索。
- 只总结截至目前已经完成的探索和结论。
## 输出要求
只返回一个原始 JSON 对象，不要输出其他内容。
拒绝任务时返回：
```json
{"accepted": false, "reason": "policy_refusal"}
```
正常返回示例：
```json
{"accepted": true, "data": {"description": "..."}}
```
## 规则
- `description` 必须是客观探索结论，不要输出解释性废话。
## 上下文
### 图快照
{graph_yaml}
### 当前 intent id
{intent_id}
### 当前 intent 描述
{intent_description}
````

### `bootstrap_conclude.md`

````md
# 背景
当前场景是授权的 AI 渗透测试比赛 / 靶场环境。
## 任务
- 不要继续推进。
- 不要等待未完成的任务。
- 只总结截至目前已经确认、且对达到 goal 最有帮助的关键事实。
## 输出要求
只返回一个原始 JSON 对象，不要输出其他内容。
```json
{"accepted": true, "data": {"fact": {"description": "..."}}}
```
## 上下文
### Origin
{origin}
### Goal
{goal}
### Hints JSON 数组
{hints}
````

### `writeup.md`

````md
# Task
You are given the successful solution path of a completed CTF / penetration-testing challenge: the ordered chain of confirmed facts and intents that led from Origin to Goal, plus — when available — the execution records (operation notes and shell commands) collected from the agent sessions that produced each step.

Write a **writeup** of this challenge. The writeup is a reproduction document: a reader must be able to redo the entire solve from scratch by following it, step by step, with nothing else.

# Output Requirements
Return only one raw JSON object. Do not output anything else.

Normal return example:
```json
{"accepted": true, "data": {"writeup": "...(markdown)..."}}
```

# Writeup Rules
- Write the writeup in Chinese (Simplified). Keep commands, payloads, file names, and technical identifiers verbatim in their original form.
- Describe **only the successful path** below. Do not mention failed attempts, dead ends, or off-path exploration.
- Structure: challenge overview (target, goal), then the steps in reproduction order, then a final conclusion with the flag / final proof.
- The `writeup` value is a single Markdown string.

# Context
## Project
{project_title}

## Origin
{origin}

## Goal
{goal}

## Successful Path (ordered steps from Origin to Goal)
{main_chain}

## Execution Records per Step
{execution_details}
````
