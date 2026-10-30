# 2026-10-29 · 回执可见性与同事链接通（super-agent + <profile-b>高手 + caixuf高手）

范围：本仓（super-agent）的交互/投递模块改动，以及它在两台机器上的两个应用
（远端宿主 `<profile-b>` = 团队测试 Bot、本机 `caixuf` = caixuf高手）的部署与实测。
本页只写**跑过、看过**的东西；未做的一律标"未做"。

## 1. 起因（现场证据，不是推测）

| 现象 | 物证 |
| :--- | :--- |
| <profile-b> 会话里"派工了但聊天看不到，结论只留在队列库" | 它自己的 T2 状态表：`t2-natural-a88ef5ab…`「派工验证：durable_submit 自然语言派单给阿证」→ **回执未投递**（2026-10-27 13:21） |
| 队列里躺着永远投不出去的回执 | 远端宿主 `analysis-queue/pool.db`：outbox 25 条、**未 ACK 21 条**、`delivered=0`；其中 **4 条 `origin=NULL`**（10-26 23:49 准入，早于路由持久化），其余目标是一次性 `api_server` 会话（早已不存在）；`origin_repairs` 空 |
| 引擎对积压**静默** | `plugins/durable_dispatch` 的 backfill 线程调了 `service.drain_delivery(...)` 却**丢弃返回值**，只在抛异常时打一行 `completion backfill failed: <类型>` |
| 同事名册"看着在、其实没用" | <profile-b> 全历史 10,627 条含工具调用的消息里 `durable_submit/status/cancel` **0 次**、`colleague_*` **0 次**；`delegate_task` 38 次但最新一次停在 10-26 14:52 |
| 名册类插件"假启用" | `colleague-catalog`/`colleague-team` 在 `plugins.enabled` 里，但缺 `plugins.entries.<name>.settings.enabled` → `register()` 首行即 return，工具面里没有任何工具 |

## 2. 本仓改了什么（`3d2e23a` 投递健康+docs/installer；`137444b` 长任务落账；`eb255c9` 本页）

1. **投递健康（引擎）** `plugins/durable_dispatch/__init__.py`
   - backfill 每趟消费 `drain` 状态，按签名去重打一条 WARNING，并把该趟记成**持久健康观察**
     （`observe_health`，scope=`durable-completion-delivery`，只带计数与结果名，**不含路由/job id/文本**）；
     积压持续 `delivery_health_fault_grace`（默认 60s）→ 入一条 `health_fault`；清空稳定 10s → `health_recovery`。
   - 新设置：`report_delivery_health`（默认 true）、`delivery_health_fault_grace`（0..3600s）。
   - 观察失败一律吞掉（`never raises`），不影响投递本身。
2. **运维 SOP** `docs/COMPLETION_DELIVERY.md`（新）
   - 只读巡检 CLI：`PYTHONPATH=<checkout> python3 -m agentcore.completion_backfill --root <queue> --profile <p>`
     （`deliverable=false` = 永远不会自动投递）；
   - 存量无主回执的**带证明管理性修复**：`Pool.attach_origin(job, origin, expected_payload_sha256, proof_id, proof_sha256)`
     的前置条件、步骤与禁止事项；
   - 现象对照表 + 健康事件落在共享 ops-alert outbox、宿主需自备发送器。
3. **安装器（交互入口真相化）** `install/install.sh`
   - ⑧ 新增：打印**以 `settings.enabled` 为总开关**的 8 个插件（`colleague-catalog` / `colleague-team` /
     `durable-dispatch` / `illustrated-delivery` / `local-model-grounding` / `markdown-delivery` /
     `memory-bridge` / `task-status`）并写明"进了 `plugins.enabled` 不等于生效"；
   - ⑫ 新增第 6 条：升级后首查逐个 durable 队列根的回执体检 + 健康告警的宿主依赖。
4. **文档** `docs/PLUGINS.md`：新增 §5（总开关插件清单）与 §6（同事类插件就绪度：
   `colleague-catalog` 可生产启用；`colleague-team` **未接实例创建/派工/发送/停用四条驱动，勿在现有系统启用**）。
5. **测试** `tests/test_durable_dispatch_backfill.py` +5 例（真实 ledger）：积压产生**一次** fault、
   重复观察不重复入队、恢复事件需去抖、poller 端到端入队并在 unload 时停、`modules={}` 时**不抛错**。
6. **测试不得写真实状态目录（引擎，同一类缺陷）** `tests/conftest.py` + `tests/test_state_isolation.py`
   - 现场实证：`agentcore.liveness.default_db_path()` 在未给路径时落到**用户真实**的
     `~/.local/state/agentcore/liveness.db`，于是用例构造的 ledger / 插件服务直接写进了运行中 profile 的账本 ——
     2026-10-29 实测本机 **561 行中 457 行** owner 是 `test-profile:*`/`session-one`。
   - 改法：会话级 autouse 夹具把 `AGENTCORE_STATE_DIR`/`AGENTCORE_LIVENESS_DB`/`AGENTCORE_LOCK_DIR`
     整体挪进 tmp（子进程继承）；新增 `tests/test_state_isolation.py` 钉住**生效值**（夹具被删/改名即红）。
   - 证据：相关 5 个测试文件 94 passed，且**真实账本行数与字节数前后一致**；全量 `verify.sh` 复跑同样前后一致。

7. **长任务账本落账（引擎，另一处同源缺陷）** `plugins/longtask_guard/__init__.py`
   - 现场证据：远端宿主 的守卫当天转后台 **27 次**，而该 profile 的 `liveness.db` **一条行都没有** ——
     `post_tool_call` 拿到的是**模型原始参数**（不含 `background`），旧判定直接 return，身份从未登记，
     `task_status` 因此无账可读。
   - 改法：**回执本身就是证据** —— spawn 回执（`output == "Background process started"` + `session_id`）
     与 yield 回执（`yielded_session_id`）各自足以证明后台身份；仅"顺口引用了那句话"的前台结果仍被忽略。
   - 测试：`tests/test_longtask_guard.py` +3 例（原始参数下的 spawn/yield 回执必须落账；引用那句话的前台结果不落账）。
     该文件 46 passed。

### 门禁（本仓自证）

```
① 静态检查（9 个 shell 脚本语法）      [PASS]
② pytest tests -q                    1239 passed, 20 skipped  （改动前 1234）
③ install/install.sh --dry-run        退出码 0（⑧/⑫ 新输出已核对）
```

## 3. 两个应用的部署与实测

| 项 | 远端宿主（<profile-b>高手） | 本机（caixuf高手） |
| :--- | :--- | :--- |
| 仓 | `8c09531 → 3d2e23a`（push + updateInstead） | 本地 `3d2e23a` |
| 资产 | `install.sh --profile <profile-b>`（幂等） | `install.sh --profile caixuf`（幂等） |
| 部署件含新代码 | `grep -c _report_delivery_health` = **2** | **2** |
| 网关 | restart → active，插件加载失败 **0** | restart → PID 281004，加载失败 **0** |
| 同事名册 | `colleague-catalog` 启用 **15 岗**（analysis/evidence/triage/replay/artifact/patrol/cases/memory/architect/backend/frontend/test/ops/reviewer/hr）；工具面 `✓ colleague_catalog`；工具实测返回 15 岗、单岗过滤得"阿证"、未知岗空、无 session 拒绝 | **未铺**（无同事 worker，铺了就是"派了没人干"；它的分派路径是 `delegate_task` 子 agent） |
| **存量修复 SOP** | 生产库副本实测：`attach_origin` 成功 + 幂等 + `deliverable` false→true；拒绝路径 3/3 正确 | 同上流程通用（本机队列 total 2 / deliverable 1） |
| **R1 端到端** | **`ops_alert_outbox` 出现 1 条 `health_fault`**（profile=<profile-b>，scope=`durable-completion-delivery`，gen=1，00:27:19，`observed={'delivered':0,'outcomes':[],'parked':18,'status':'empty'}`）+ `ops_health_conditions` 一条 active episode；`agent.log` 有 `completion delivery backlog: parked=N` | 无（`deliver_completions: false` → 根本没有投递 poller，也就没有健康记录；队列盘点 total 2 / deliverable 1） |
| **长任务账本** | 修复前：`liveness.db` **不存在**。修复后：**出现 6 行**，含经网关真跑的一条 `('proc_ccf158b9c2ce', '<profile-b>:api-…', 'sleep 25; echo guarded-probe', 'running', 693209, 1)` | 本机账本 561 行，但其中 **457 行是测试 owner**（见 §2.7；真正的 profile 行约占 1/6），17 条 `running` 属死进程，退出读取时才现算 |

> 远端宿主 的健康事件**证明了整条链**：poller 看到了 18 条永远投不出去的回执 → 去抖 60s → 入队一条
> 持久 fault 事件。这正是过去"静默重试、没人知道"的那一段。
> 远端宿主 的账本行**同样证明了第二处修复**：真派一条会被守卫转后台的命令（走该 profile 的
> `api_server`，不打扰消息平台），账本立刻出现该进程 —— 修复前同一条路径一条都不落。

## 4. 未做 / 待决策

1. **21 条存量回执的修复**：按 `docs/COMPLETION_DELIVERY.md` §2 需要**取证**（原会话路由 + payload sha256 + 取证人签核）。
   **实测更正**：未 ACK 21 条里 **18 条无 route**（早前只抽样到 4 条），其余目标会话已死。
能不能修由部署方定；**流程本身已在生产库副本上验过**：
`attach_origin` → `attached=true`、重放幂等（`already_attached=true`）、修完 `deliverable` 由 **false 翻 true**；
三条拒绝路径也验过（payload sha 不符 / origin 无 route / 已被占用且证明冲突）。
副本在 `/tmp/qcopy`（一次性，验完即删）。
2. **ops-alert 发送器**：两台机器都没有配（`OPS_ALERT_*` 未见、无发送器单元）→ 健康事件目前**只落在库里 + 一条 WARNING**，没有外部送达。
3. **长任务账本的退出回收**：远端宿主 的账本已从"不存在"变为**有行**（见 §3），但行停在 `state=running`
   —— 退出由 `task_status` 读取时现算；两台机器都**没有 liveness 守护单元**，无人主动 sweep。
   本机那份账本另有**测试污染**问题（见 §2.7 已修）：561 行里 457 行 owner 是 `test-profile:*`/`session-one`,
   只有约 1/6 是真实 profile 的行；要不要清理这批历史噪声，属数据删除，部署方定。
4. **同事派工纪律**：`durable_submit` 至今 0 次调用；名册与工具面现在都在，是否要求"某类活必须派"属部署方（SOUL）决定。
5. **caixuf 的 `deliver_completions=false`**：与"回执可见"目标冲突 —— 要么打开投递，要么把 ⑫ 第 6 条体检列进常规巡检。

## 5. 环境事实（本次核实，供后续轮次不要踩）

- **在线保持机制**：网关 user 单元是 `Restart=always` + `RestartSec=5` + `StartLimitIntervalSec=0`，
  配合 `loginctl enable-linger` —— 机器开着、任何退出都会在 5s 内被系统拉起（这就是"主 agent 掉线不久"的机制）。
  本次全部 `restart` 都走它，未发现**任何重复网关进程**（每 profile 仅 1 个，`HERMES_HOME` 各自正确）；
  `gateway-starts.log` 最近 6 次启动时刻与本次操作逐一对得上，无外来启动。
- Windows 侧无 hermes 相关计划任务（`SpaceAgentTask` 是系统 `SpaceAgent.exe`，与 agent 无关）；无 root 级守护单元。
- 本机 `mcp_death_supervisor.py` 随网关子进程运行（只回收 MCP 子进程）。
- 两台机器都没有 ops-alert 发送器、没有 liveness 守护单元。