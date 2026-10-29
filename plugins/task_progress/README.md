# task-progress — 一个长任务，一条可编辑的进度

给「长任务」一个**固定入口**：任务起时发一条消息，之后**改同一条**，不再每条工具输出都开新消息。

## 行为

| 时点 | 行为 |
| :--- | :--- |
| 后台进程被启动（`terminal` 回执里出现 `session_id`/`yielded_session_id`） | **发一条**进度条目（状态：已接收/进行中），并把这个任务登记进 `cards.db`；**同一回合的后续后台任务合并进同一条**（条目 key = `turn:<turn_id>`） |
| 存活账本出现真实变化（state / phase / progress / verdict） | **编辑同一条**；终态（`done`/`failed`/`cancelled`/`lost`/`stalled`）只写一次 |
| 只有时间在走、没有新证据 | 每 `min_interval`（默认 60s）最多编辑一次，仅推进「已用」；**不编造百分比/预计完成** |
| 账本无记录 | 显示「暂无新进展」并附任务 id，不猜 |
| 宿主没有会话（无 conversation） | 不发、不崩，日志留一行 |

身份与终态持久化复用 `agentcore.card_store`（`cards.db`：任务/消息 id/终态）；编辑顺序由单线程 poll 保证；"能说什么"的判据在
`agentcore.task_progress`（纯逻辑，单测覆盖）。

## 命令（不进模型回合）

宿主在**回合之前**分发已注册的 `/name` 命令，并在会话环境作用域内调用处理器 —— 所以查进度
不需要一次模型调用，也不依赖模型"想起来去查"：

| 命令 | 作用 |
| :--- | :--- |
| `/longtasks` | 本会话登记过的长任务：状态 · 已用 · 当前阶段（默认含已终态，最多 20 条） |
| `/longtask <任务号>` | 单个任务的状态（证据同一条目文本）；别的会话的任务一律 `unknown_task` |

名字带 `long` 前缀是为了**避开宿主内置命令**（`/tasks`、`/task`、`/status`、`/progress`、`/goal` 都已存在，内置优先）。

不做：暂停/继续/取消 —— 那需要 worker 侧控制面与宿主 steering，属阶段二未完成项。
自然语言问"还在做吗"仍可走 `task_status` 工具，但**模型回答不等于控制动作已完成**。

## 设置

```yaml
plugins:
  entries:
    task-progress:
      settings:
        enabled: true                      # 必须，否则不注册
        engine_root: /absolute/agentcore-checkout
        liveness_db: /absolute/liveness.db # 与 task-status / longtask-guard 同一个账本
        poll_seconds: 10                   # 轮询间隔（最小 2s）
        min_interval: 60                   # 无变化时的最小编辑间隔（0 = 只在真变化时编辑）
        max_tracked: 20                    # 同时跟踪的上限（超出不再开新条目）
        store_path: /absolute/cards.db     # 可选；默认 <plugin data dir>/cards.db
        resume_after_restart: true         # 重启后尝试编辑原条目，而不是再发一条
```

`enabled` 是硬开关。关闭后重启网关即完全静默。

## 与宿主的关系

- 发送只用宿主的 **公开** 接口 `PluginContext.post_message(...)`，句柄的 `edit(text)` 原地更新；
  插件不自建平台客户端、不复制凭据。
- 重启后内存句柄丢失：插件用宿主插件消息桥（`gateway.plugin_messages.resolve/edit`）**再编辑同一条**；
  该能力不可用时记录 `entry_frozen`（写进插件状态），**绝不**再发第二条。
  这一处依赖宿主内部模块，属升级需复验项（见下）。
- 本插件**不**启动/唤醒模型回合：不调用 `inject_message`，不改变 `notify_on_complete`。

## 已知边界（第一版，按指导的取舍）

1. **一个回合一个条目**：同回合的多个后台任务合并显示（`本轮 N 个后台任务`），终态取"最坏"：任一失败即失败、有完成即完成、全取消才取消；跨回合不合并。
2. **重启后的编辑**依赖宿主桥；不可用时该条冻结（不重复发、不冒充同一张卡）。
3. **不做** 暂停/取消控制面（阶段二）、不做结果文件交付（阶段三）。
4. 与网关自带的「忙时心跳/逐工具进度」是**不同层次**：那是每回合的，这里是一任务一条。

## 升级复验清单

宿主升级后必须重跑：

1. 探针：`PluginContext.post_message` 返回对象仍有 `ok` / `message_id` / `edit`；
2. 探针：`gateway.plugin_messages.resolve` / `edit` 签名未变（重启续写依赖它）；
3. `agentcore.card_store` 的 `bind/load/writer/active/save` 签名未变；
4. 隔离环境冒烟：起一个后台进程 → 只有一条消息 → 账本 phase 变化引发一次编辑 → 终态一次编辑；
   重启后不新增第二条（冻结或续写二选一，且日志可证）。
