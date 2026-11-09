# 长任务交互优化：交接记录（2026-10-29）

遵循 `docs/LONG_TASK_INTERACTION_GUIDE.md`。本页只写**跑过、看过**的东西；没做的逐条列在 §4。

## 0. 开工前只读核实（结论）

| 项 | 结论 | 证据 |
| :--- | :--- | :--- |
| 仓 | `master` = `63c0a71`；除指导文件本身外工作区干净 | `git status` / `git log` |
| 检出 | `agentcore` 是另一份旧检出，**未使用**；改动全在 `super-agent` | 指导原文 + 目录核对 |
| 目标档 | `caixuf`；网关 `hermes-gateway-caixuf`（user systemd，PID 1170960，active） | `systemctl --user show` |
| 忙时模式 | `queue` | `display.busy_input_mode` |
| 平台显示 | 流式 **on**；工具进度 **off**；忙时明细 **on**；语言 **zh**（本轮新设） | `display.platforms.feishu` |
| 回合心跳 | `agent.gateway_notify_interval = 60`（本轮新设；此前未设 → 默认 180） | `config.yaml` |
| durable | `deliver_completions: false`（回执不投递；与 `docs/COMPLETION_DELIVERY.md` 的注意一致） | `plugins.entries.durable-dispatch.settings` |
| 存活巡检 | `agentcore-liveness-sweep.timer` active（5 分钟，只分类） | `systemctl --user is-active` |
| **卡片基础模块** | `task_card` / `card_store` / `card_emit` / `card_stream` / `session_execution` 在**非测试代码里零引用**（此前是"睡着"的库，只有测试） | `grep` 全仓 |
| 宿主插件接口 | `PluginContext.post_message`（返回句柄：`ok`/`message_id`/`edit`/`aedit`，`edit` 线程安全且会切到网关 loop）、`apost_message`、`inject_message`、`PluginState`（profile 级 JSON）、`spawn_task`；**没有**插件级卡片 API | 宿主 `hermes_cli/plugins.py`、`hermes_cli/plugin_messages.py` |
| 重启续写 | 可用宿主桥 `gateway.plugin_messages.resolve/edit`；仓库自己的 `runtime/hermes_completion_transport.py` 已是同一用法 | 源码核对 |
| 宿主 Goal | `hermes_cli/goals.py`、`gateway/run_goals.py` 存在；**真实入口未验证**（见 §4） | 文件存在 ≠ 消息入口可用 |

## 1. 基线（真实聊天记录，近 12h，消息平台）

- 回合 32 次；`text_response(stop)` 27、`background_review superseded` 3、`gateway_shutdown` 2；**fallback 0**。
- 回合时延 p50 **133s** / p90 **624s** / max 1339s；每回合 API 调用均值 **19.4**、p95 **53**。
- 自动心跳**确实在工作**（`⏳ Working — N min — waiting for provider response (streaming)`，间隔 60s）；此前用户看到英文是因为 `display.language` 未设（默认 `en`），`locales/zh.yaml` 已 100% 覆盖。
- 12h 聊天构成（app 45 / user 5）：`Cronjob Response` 4、`【自动归因】` 7、`Hermes is shutting down` 3、`This message was recalled` 4。
- 今早两次 `systemctl restart`（09:09:31 / 09:13:43），第二次停止超时被 SIGKILL（09:14:58），打断其自身回合；非本会话所为。

## 2. 本轮实现（阶段一：任务状态 + 单一展示入口）

新增：

- `agentcore/task_progress.py` — **纯策略**：证据-only 快照（state/phase/progress/artifacts/heartbeat）、
  "何时该更新"（真变化 / 终态 / 每 `min_interval` 推进已用时间）、**终态 write-once**、文本渲染
  （无百分比时不给、无"下一步/预计完成"）。
- `plugins/task_progress/` — **接线**：后台进程启动时**发一条**条目；之后**编辑同一条**；
  终态一次；无会话不发不崩；重启不重复发（有 route 时记录并尝试续写，不可用则 `entry_frozen`）。
  复用 `card_store`（身份/序号/终态）、`card_emit`（视图）、`liveness`（证据）。

- 条目按**回合**聚合：同一回合启动的多个后台任务共用一条（`turn:<turn_id>`），显示"本轮 N 个后台任务"，
  终态取最坏（任一失败即失败；有完成即完成；全取消才取消），已用时间取最长。
- `plugins/task_progress` 另外注册**两个不进模型的命令**（阶段二的第一小块）：
  `/longtasks`（本会话长任务清单：状态·已用·阶段）与 `/longtask <任务号>`（单任务状态，跨会话一律 `unknown_task`）。
  名字带 `long` 前缀是**实测撞名后的修正**：宿主已有 `/tasks`、`/task`、`/status`、`/progress`、`/goal`，内置优先。
  宿主在回合前分发 slash 命令并在会话作用域内调用，因此查进度不产生模型回合 —— 这是对"别依赖模型
  被动发现"的机制回应。

明确不做（按指导的阶段二分界）：不启动模型回合（不 `inject_message`）、不改 `notify_on_complete`、
不做暂停/取消控制面、不做附件交付。

## 3. 验证矩阵

| 层 | 内容 | 结果 |
| :--- | :--- | :--- |
| 单测（策略） | 证据-only、无百分比不编、change/interval 判定、终态不被后续帧重开、渲染边界 | 5 passed |
| 单测（插件） | 一条消息→编辑同一入口→终态一次→静默期不刷；lost 报"未完成"；无会话不崩；重启不重复发；有 route 时持久化并在无网关时冻结；registry 退出码回落；无 route 已完成条目收口；`/tasks`、`/task` 会话隔离且不启模型 | 9 passed |
| 仓门禁 | `bash verify.sh`（静态 + 全量 pytest + 安装器 dry-run） | 见 §6 |
| 真实（CLI 回合） | `hermes -p caixuf -z` 触发后台 spawn → 插件 post 成功（`ok=True`，`message_id=plugin-message:task-progress:…`），`cards.db` 落绑定 | 通过 |
| 真实（API 回合） | 同一条命令走 `api_server`：**`post_tool_call` 钩子未分发**（宿主行为），因此该平台不会开条目 | 已记录（见下） |
| 真实（slash 命令） | `hermes -p caixuf -z "/longtasks"` → 「本会话没有登记过的长任务。」；`/longtask proc_deadbeef0000` → `unknown_task`；`/longtask`（无参）→ 用法 | 通过（宿主在回合前分发，未产生模型回合） |
| 真实端到端（消息平台） | 用户在消息平台发 `pytest -q（timeout 300）`：10:35:17 **一条**条目（命令 + 状态），10:38:38 **同一条被原地更新**为「状态：已完成 · 已用（最长）3分20秒」；平台 `update_time` 与插件 `last_delivery.edited=true` 双证；全程无第二条进度消息 | **通过** |

### 这次真跑暴露并已修的两处

1. 单任务条目的标题与成员行**重复**显示同一条命令（`_title` 与成员行各一次）→ 改为标题固定「任务」、成员行承载命令；
   账本里无信息量的阶段（`started`/`queued`/`running`）不再展示。
2. **"已完成"指进程退出成功，不等于业务结论**：该次任务是 `pytest -q | tail -1`，管道退出码来自 `tail`（0），
   所以条目如实报"已完成"，而 bot 在正文里报"2 failed"。条目只声明它有的证据；业务结论仍由正文承载。

### 已记录的宿主行为

- **`api_server`（无状态 HTTP）回合不分发 `post_tool_call` 插件钩子**：同一命令经 `api_server` 触发时
  插件收不到（`cards.db` 无记录、日志无 `plugin_message … op=post`）；经 CLI 回合同样命令则正常。
  结论：进度条目只覆盖有会话的消息平台/CLI 回合；无状态 API 调用不算"用户会话"，符合预期但不明显，已记录。
- 插件注册即建库（`plugin-data/<plugin>/cards.db`），注册成功与否可直接以该文件是否存在判定。

## 4. 未做 / 明确未验证

1. 宿主 **Goal** 的真实入口（创建/状态/暂停/恢复/重启恢复/会话绑定）——仅有源码存在，未做消息面验证。
2. 重启续写的**真实链路**（网关运行期 `gateway.plugin_messages.resolve/edit`）——单测只覆盖"不可用时冻结"。
3. 阶段二：主会话与任务执行分离、补话送达执行中步骤、暂停/继续/取消、并发重试防重复创建。
4. 阶段三：重启后的续跑决策、唯一最终发送方、附件真实上传核验。
5. 一个"用户任务"聚合多个后台进程（当前一进程一条目）。
6. 三个业务推送源（定时任务/自动归因/撤回）对聊天信噪比的影响 —— 属业务侧配置，未动。

## 5. 验收步骤（给用户，5 分钟）

1. 在消息平台对 `caixuf` 发：`跑 pytest -q（timeout 300）`——应被守卫转后台；
2. 期望：**一条** `任务 … 状态：已接收`；中途问「还在做吗」应看到**同一条**被更新；
3. 结束后期望同一条变为 `已完成` / `未完成（进程已不在）`；不应再出现第二条进度消息；
4. 网关自带的 `⏳ 正在处理 — N 分钟` 是**回合级心跳**，与本条目不是重复；
5. 失败判据：出现两条及以上进度消息、或终态后又被改回"进行中"、或百分比/预计时间被编造。

## 6. 门禁（`bash verify.sh`，本轮提交之上）

```
① 静态检查（9 个 shell 脚本语法）        [PASS]
② 单元测试（python3 -m pytest tests -q） 1279 passed, 20 skipped
   （在 `0fd3abe` 的**干净 worktree** 上重跑：不受工作区里他人未提交改动影响）
③ 安装器 dry-run（零副作用）             退出码 0
结果：全部通过
```

任务进度的定向用例（策略 6 + 插件 9 = 15 条）在最终提交 `0fd3abe` 上随全量套件通过；跳过项沿用仓库既有原因
（可选依赖缺失 / 需要真实宿主解释器），不当作通过。
