# T 任务收尾测试报告：caixuf canary + 实际 profile 实测

日期：2026-10-28。范围：本地 `caixufcanary` 隔离 profile + **实际 caixuf profile**（用户授权）+ 通用仓测试；**未触碰 远端宿主**。caixuf 侧仅补齐 liveness 插件设置并重启网关。

## 一、结论

- T1–T9 的可执行机制已在本地隔离环境验证；T6 干净室从“1 项失败”修到**全部通过**。
- 真实 agent 任务验证了 8 类工作：工具调用、长任务后台化、活体状态查询、kill 后对账、代码+测试、文档渲染、durable 提交、缺文件诚实报错。
- 仍未完成的是**真实渠道验收**：T1 真实并发消息 UX、T5 真实用户消息同卡、T6 发布推送、T7 生产启用、T8/T9 真实通知渠道。T10 在仓内没有独立定义，无法验收。
- 本报告只记录本次可复现证据；工作区里并行的同事/交付面改动未回退。

## 二、测试环境

| 项 | 值 |
| :--- | :--- |
| 宿主 | 本地 caixuf 工作站 |
| 隔离 profile | `~/.hermes/profiles/caixufcanary`（clone 自 caixuf，插件/队列/记忆/账本指向独立 `state/`） |
| 模型 | 继承 caixuf 的本地 provider，失败时回退第二 provider |
| 禁用 | 远端宿主 未连接；caixuf 主 gateway 在补齐 task-status 设置后重启两次（active_agents=0）；canary 临时 gateway 在测试前清理 |
| 账本 | `state/liveness.db`；队列 `state/analysis-queue`；记忆 spool `state/memory-spool` |

## 三、真实 agent 任务矩阵

| # | 任务设计 | 期望 | 实测 | 判定 |
| :--- | :--- | :--- | :--- | :--- |
| C1 | 只回复 `CANARY_OK` | 精确输出 | 输出 `CANARY_OK` | PASS |
| C2 | 前台 `sleep 5` 不显式 background | guard 自动转后台、立即返回句柄 | 返回后台进程 pid，未阻塞 5s | PASS |
| C3 | 后台 `sleep 30` 后立刻 `task_status list` | 返回 running + elapsed + heartbeat age | `proc_...`，state=running，elapsed 2.4s，heartbeat 2.4s | PASS |
| C4 | 后台 `sleep 120` → `process_manage kill` → `task_status inspect` | 观察到退出，不得假成功 | state=failed，exit=-15，detail=host process exited | PASS |
| C5 | 建 `calc.py` + `test_calc.py` 并跑 pytest | 文件落盘、测试通过 | 2 文件存在；`1 passed` | PASS |
| C6 | 生成 table/mermaid 文档并渲染 | HTML + PNG 真实产物 | HTML 3,584,509 B；PNG 85,537 B；1 page / 1 diagram | PASS（视觉像素检查被外部 vision 后端 `no_db_connection` 挡住） |
| C7 | `durable_submit` + `durable_status` | 入队、返回 id、可查状态 | id `85c9...`，state=queued，elapsed 2.3s，heartbeat 2.4s | PASS |
| C8 | 读取不存在的文件 | 如实报错、不编内容 | `File not found: /tmp/definitely-missing-canary-file` | PASS |

补充（T2 真实 worker）：在 canary 队列上启动 `agentcore.worker.Worker`，用确定性 fake model 跑 `analysis_runner.py`。job `85c9...` 从 queued → completed，tries=1，产出 `report.json/report.md/manifest.json`；`durable_status` 返回 `completed`、`elapsed_s=1177.4`、`heartbeat_age_s=0.0`、`verdict=done`，ledger 同步为 done。这里验证的是队列/租约/产物/状态链路，不是模型分析质量。

补充：C5 暴露宿主 venv 无 pytest，agent 自行切到系统 Python 完成；这属于环境事实，已记录在回执中。

## 三-B、实际 caixuf profile 实测（非仅 canary）

在用户授权的本地 caixuf profile 上完成：

1. `task-status` 只在 `plugins.enabled`，缺少 `entries.task-status.settings.enabled/engine_root/liveness_db`，导致网关加载但插件不注册。补齐设置后重启 caixuf 网关（两次，`active_agents=0`，每次约 25s）。
2. 新会话调用 `task_status action=list` 返回 `{"ok": true, "tasks": []}`，工具真正出现在工具面。
3. 实际启动 `sleep 20`：agent 回 `task_id=proc_fec871a74e09 state=running elapsed_s=2.5 heartbeat_age_s=2.5`；默认账本 `~/.local/state/agentcore/liveness.db` 有对应 running 行。
4. 实际启动 `sleep 120` 后 `process_manage kill`，`task_status inspect` 返回 `failed`、`exit_code=-15`，不是假成功。
5. 额外只读检查：`hermes --profile caixuf -z 'Reply with exactly: CAIXUF_PROFILE_OK'` → `CAIXUF_PROFILE_OK`。
6. 实际 caixuf 编码任务：临时目录建 `hello.py` + `test_hello.py`，agent 用系统 pytest 跑出 `1 passed in 0.01s`，两个文件落盘。说明实际 profile 的工具循环、文件写入与测试执行可用。
7. 实际 caixuf durable 生命周期：`durable_submit` job `0a29...` → `durable_status` 返回 `queued`、`elapsed_s=2.2`、`heartbeat_age_s=2.2`、`verdict=running` → `durable_cancel` 接受取消请求（`cancellation_requested=true`，尚未 terminal）。说明 T2 的提交/状态/取消在真实 profile 可用。
8. 实际 caixuf 队列 worker 端到端：提交 job `c5cc...`（queued）→ `agentcore.worker.Worker` 领取执行 → status `completed`、`elapsed_s=0.3`、`heartbeat_age_s=0.0`、`verdict=done`；结果 `ok=true`；ledger 同步 `done`。说明真实 caixuf 队列的 claim/lease/runner/产物/状态链路可用。
9. 实际 caixuf 带图文档：`note.md`（标题 + 3 行表 + mermaid）→ HTML 3,584,881 B、PNG 138,808 B（1500×2125，6766 色），render.json 显示 1 diagram / 0 errors / 0 warnings；agent 还通过文档交付链路生成了一个在线文档链接。PNG 视觉像素核对仍受 vision 后端 `no_db_connection` 限制，结构/PIL 尺寸检查通过。

结论：liveness 已从 canary 验证推进到**实际 caixuf 工具面**。未触碰 远端宿主；caixuf 网关重启是本次唯一生产侧动作。

补充（T7 实际 profile 只读）：`python3 tools/memory_budget_report.py --profile-dir ~/.hermes/profiles/caixuf --json` → memory 1317/20000（7%）、user 71/8000（1%）、pending 0、over85=false。生产记忆处于健康区间；这不等于保全门禁已启用。

## 三-C、T1 同会话队列实测（canary）

同一 session `local-16ab...`：

1. 后台发起：`Run sleep 15 in foreground, wait, then reply FIRST_DONE`。
2. 3 秒后同一 session 发起：`Reply exactly SECOND_DONE`。
3. 日志顺序：第一条 inbound 19:37:04 → 第二条 inbound **19:37:26**（第一条完成后才被受理）；第二条总耗时 22s；输出分别为 `FIRST_DONE` / `SECOND_DONE`，没有插队或串话。

这证明 `busy_input_mode: queue` 的 FIFO 行为在真实网关生效。未做的是真人多客户端并发 UX。

## 四、T1–T10 状态

| 任务 | 本次证据 | 状态 |
| :--- | :--- | :--- |
| T1 前台保护/忙时消息 | guard 43 passed；C2 自动后台化通过；实际 caixuf 的 `sleep 20` 后台化 + `task_status` running 通过；宿主 queue FIFO 测试 22 passed，3 个 async 用例因缺 pytest-asyncio 未跑；`busy_input_mode` 实际为 queue；同会话并发测试 FIFO 通过（第二条 22s 后受理） | 部分；真人多客户端 UX 未观测 |
| T2 持久分析入口 | durable 37 passed；C7 入队/状态通过；真实 worker 跑完整 job：worker completed，产物 report.json/report.md/manifest.json，ledger done，status 返回 completed + elapsed + heartbeat；实际 caixuf 的 task-status 工具面已加载 | 端到端机制通过；真实模型质量未验证（使用确定性 fake adapter） |
| T3 自动记忆闭环 | memory 202 passed，1 skipped（真实 host 布局）；隔离写入→召回链路已有 | 隔离通过；生产钩子未启用 |
| T4 带图文档 | arch_doc 11 passed（补齐 markdown/websocket/PIL/Pygments 后）；C6 真实 HTML+PNG | 机制+本地产物通过；视觉人工校样未做 |
| T5 同卡终态 | card 39 passed；`verify_card_finalize.py` 在 `HERMES_FEISHU_CARD_V2=1` 下 18 项 **ALL PASS**；实际 caixuf `platforms.feishu.card_replies=true`，`display.platforms.feishu.streaming` 未设置 | 机制+配置通过；真实用户消息的同卡观测未做 |
| T6 发布整理 | `bash verify.sh`：1234 passed / 20 skipped / 0 failed；当前树 Docker 干净室**全部通过**；`tools/t6_gap_audit.py` 16 项中 2 OPEN：工作区有未提交改动、HEAD 无 release tag | 干净室/安装通过；提交、tag、推送、凭据仍未执行 |
| T7 记忆保全 | memory 202 passed；`test_memory_conservation_real_host.py` 1 passed（修正 `.venv`/`venv` 探测） | 机制+真实宿主锁通过；生产启用与逐条审阅未完成 |
| T8 漂移告警 | ops/capability 136 passed（outbox/daemon/sender/guard） | 机制通过；未接真实通知目标 |
| T9 自学习 | learning 153 passed（候选/抓取/晋升/检索/审查/撤销） | 机制通过；真实渠道与自动召回未接 |
| T10 | 仓内无独立定义/报告 | 未知，需任务板原文核对 |

## 五、本次发现并修复的缺陷

1. **后台启动回执 `exit_code=0` 被当成完成**：guard 在 `yielded_to_background`/`running` 时不再 `record_exit`；新增反例测试。
2. **账本首次并发初始化留下 0-byte 文件**：`LivenessLedger` 加 sidecar flock + `ensure_schema` 重试 + 工具侧自愈；20 进程并发初始化实测有表。
3. **tool handler 与 hook 的 session 身份不一致**：`task_status` 增加 `HERMES_SESSION_ID` 回退，列表/查询不再错判 `unknown_task`。
4. **durable 活体写入全局库、owner 不一致**：统一 `profile:session` owner，支持 `liveness_db` 与 `engine_root` 回退；`durable_status` 输出 elapsed/heartbeat/phase/progress/verdict。
5. **pool 状态缺心跳字段**：`Pool.status` 增加 `heartbeat/created/deadline`；新增 `observe()` 按真实 worker 心跳记录，不伪造当前时间。
6. **干净室因业务桥缺失而红**：`test_dev_runner` 的真实 orchestrator 用例在无业务 checkout 时 skip，不再让 T6 干净室失败。
7. **并行报告里的业务字面量**：`docs/reports/2026-10-28_all_four_tasks_completion_and_verification_report.md` 的 `<business-platform>` 一词改为占位符，中立性门禁恢复 13 passed。

## 六、验证命令与结果

```text
# 仓自检
$ bash verify.sh
① 静态检查: PASS
② 单元测试: 1234 passed, 20 skipped
③ 安装器 dry-run: 退出码 0
全部通过 [PASS]

# 干净室（当前工作树）
$ docker build -f docker/Dockerfile -t super-agent-canary .
$ docker run --rm super-agent-canary
== 结果
  全部通过 [PASS]（干净室可部署）

# 关键套件 10 轮
liveness + daemon + task_status + guard + durable
10/10 PASS；每轮 86 passed

# T4 / T5 / T7
tests/test_arch_doc.py                         11 passed（补齐可选依赖）
tests/test_card_*.py + task_card + card_emit   39 passed
verify_card_finalize.py (CARD_V2=1)            18 项 ALL PASS
test_memory_conservation_real_host.py          1 passed

# T8 / T9
ops/capability                                  136 passed
learning                                        153 passed
```

## 七、仍未完成 / 不能声称

- **T1 忙时模式**：`display.busy_input_mode` 实际为 queue；宿主 queue FIFO 22 passed；同会话真实网关测试 FIFO 通过；真人多客户端 UX 未观测。
- **T5 真实平台**：实际 caixuf `platforms.feishu.card_replies=true`，`display.platforms.feishu.streaming` 未设置；没有真实用户消息的“同一 message ID”观测。机制和 fake adapter 只是前半段。
- **T6 发布**：干净室与 `verify.sh` 绿，但 tag、远端推送、凭据轮转、真实网关消息收发未做。
- **T7 生产启用**：门禁未在生产 profile 启用；216 条旧事实逐项审阅未完成。
- **T8/T9 真实渠道**：没有真实告警出站、没有真实检索/自动召回链路。
- **T10**：仓内无定义。
- **C6 视觉校样**：渲染产物结构正确，但 vision 后端 `no_db_connection`，未做像素级人工确认。

## 八、结论口径

可以说的：**通用机制、隔离 canary、干净室、关键长任务链路已实测通过**。

不能说的：**十个 T 任务已全部生产验收**。当前仍缺生产窗口、真实平台/渠道和发布授权；这些不是再跑单测能替代的。
