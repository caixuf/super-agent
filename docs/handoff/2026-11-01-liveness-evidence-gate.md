# 运行时证据闸门 — 收尾记录

日期：2026-11-01，Asia/Shanghai。
状态：代码已提交并推送 `main`（`a017910`），本机与 emily 均已同步；emily 的独立全量复跑在进行中，结果尚未回来。

## 1. 接手时的事实

Codex 那条 memory 线在 13:31 已经收尾（`task_complete`，`b5d17ce` 推上 `feat/autonomous-memory-p1-20261101`）。
工作区另外 15 个文件仍停在未提交状态，最后改动 11:03，Codex 明确保留其原哈希未动。这批就是本记录的对象。

接手时的三个尾巴：

1. 15 个文件改完但未验证、未提交；
2. 本地 `main` 落后 `origin/main` 两个提交（`e7dd078`、`0cd78cf`），且这两个提交也动了 `plugins/task_progress/`；
3. `plugins/task_progress/__init__.py` 依赖同目录 `legacy_messages.py`，而 profile 里部署的副本没有这个文件。

## 2. 这批改动本身

主体是**运行时证据闸门**：一个执行方说"做完了"是一句声明，任务在启动时登记的验收条件是另一份东西。
在此之前账本相信第一句，于是产物从未出现的任务仍然显示为已完成。

- `agentcore/liveness.py`：`register()` 接受 `expects`（file / exit0 / artifact）并持久化；
  `finish(state="done")` 重新核对，不符则落到新状态 `unverified` 并把未满足原因写进 detail；
  `unverified` 不是终态，`verify()` 在证据补齐后结算为 `done`；它不接受心跳、tail 更新或再次 finish。
  旧库就地补 `expects` 列。
- `agentcore/task_progress.py`：`unverified` 显示为待验收，聚合结果不算终态（不打印完成时间）；
  停在等人决策的任务不再掉进 `done`；账本给出无法识别状态词时聚合为 `unknown` 而非 `done`。
- `agentcore/proactive_watch.py`（新）：observe 档定时看一圈，只记录"本来会做什么"，不发送不动手。
- `plugins/longtask_guard/`：调用方显式写死 `timeout` 且不超过 `LONGTASK_GUARD_FOREGROUND_TIMEOUT`（默认 180s）时留在前台，
  因为等待已不是隐藏的，改成后台会把对方要的退出码换成一句"已启动"。
- `plugins/illustrated_delivery/`：经聊天媒体通道送达的完整文档（html/pdf）算交付，PNG 预览仍不算。
- `install/install.sh` + 两份 runtime 文档：`restart_drain_timeout` 的真实键是 `agent.`，
  文档写成 `gateway.` 前缀，安装器照它设置等于什么都没设。

顺手删掉两处死代码（`_HEAD_ICON = {}`、`_DOC_EXTS`），全仓无引用。

## 3. 验证

| 阶段 | 命令 | 结果 |
| :--- | :--- | :--- |
| 定向 | 6 个相关测试文件 | 188 passed，exit 0 |
| 全量（feat 基点） | `/usr/bin/python3 -m pytest tests -q` | 2083 passed / 46 skipped，279s，exit 0 |
| 全量（对齐 origin 后） | 同上 | 2047 passed / 47 skipped，267s，exit 0 |
| 全量（合入 memory 线） | 同上 | 2106 passed / 47 skipped，279s，exit 0 |
| 全量（合入上游卡片补丁） | 同上 | 2107 passed / 47 skipped，284s，exit 0 |

测试环境约束（不设就会假红）：`TMPDIR` 必须是短路径（memory recall 的 unix socket 有 108 字节上限），
`SUPER_AGENT_MEMORY_FILE` 指向空文件（否则 golden 场景测的是宿主记忆文案），
且要 unset `LONGTASK_GUARD_NOTIFY` / `_HEARTBEAT` / `_OFF`（profile .env 会让"断言默认值"的用例假红）。

端到端实跑（不是只有单测）：注册一个登记了「文件 ≥10 字节 + 退出码 0」的任务，
声明 done 而文件不存在 → 落 `unverified`，detail 记「待验收：文件不存在或小于 10 字节」；
补上文件后 `verify()` → `done`，changed=True。反例（证据始终不来）`verify()` 返回 changed=False，状态留在 `unverified`。

## 4. 分支与部署

`b5d17ce`（memory 线）与 `e7dd078`（origin 的 card transport 线）都是从 `89b95f7` 分出来的兄弟分支，互不重叠。
本次把它们合并成一条线：`4df91fc`（证据闸门）→ `2736c15`（合 memory）→ `a017910`（合上游卡片补丁），已推 `main`。

部署侧：

- 本机 profile 的 `task-progress` 与 `illustrated-delivery` 插件副本原先落后于仓库，`task-progress` 还缺 `legacy_messages.py`
  （新代码会 import 它，缺了直接报错）。已同步并在 14:2x 热重载网关：`reloaded=true`，3 个适配器重接，无 error。
- emily 的检出停在 `56fea05`，同一批改动以脏树形式存在。已先备份（`~/backup/dirty_20261101_141550.patch`、
  `dirty_tree_20261101_141550.tar.gz`）再丢弃，快进到 `a017910`，工作区干净。

## 5. 未完成与风险

- **emily 的独立全量复跑尚未回来**。本机四次全量全绿，但两台机器的宿主依赖不同（emily 的 real-host 用例会真跑），
  不能把本机的绿当成 emily 的绿。
- Codex 自报的「偶发失败根因未确认」仍未查清。本次四次全量没有复现，但这不等于它不存在。
- `unverified` 是**新增状态**，任何按 `state NOT IN ('done','failed','cancelled')` 自行判断"还在跑"的旧读法会把它算作活动任务。
  仓内已全部改成显式排除，仓外如有消费方需自行核对。
