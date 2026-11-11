# Super Agent 架构与使用 Wiki

> **文档导航**：[Wiki 首页 (Home)](Home.md) │ [01. 单根受管部署与极速上手](01-Quickstart-and-Install.md) │ [02. 业务知识叠加与服务编排](02-Business-Overlay-Guide.md) │ [03. 研发专家协同与状态图引擎](03-Multi-Agent-and-StateGraph.md) │ [04. IM 交互卡片与交付机制](04-Feishu-Cards-and-Delivery.md) │ [05. 运维体检与排障对账](05-Operations-and-Troubleshooting.md)

---

## 1. 按问题查（我想……）

读者可按实际工程场景快速直达对应章节：

| 实际工程诉求 | 推荐阅读章节 | 核心机制与证据源 |
| :--- | :--- | :--- |
| **我想亲眼看任务如何防丢单、防重复执行与防裂脑接管** | [§2 核心闭环实测](#2-60-秒亲手跑通核心闭环零依赖真机实测) 与 [01 部署指南](01-Quickstart-and-Install.md) | `Pool` 幂等去重、租约过期接管、迟到 attempt 围栏拦截与 `EX_TEMPFAIL` 临时重排 |
| **我想在空目录试装一套环境并观察单根目录变化** | [01. 单根受管部署与极速上手](01-Quickstart-and-Install.md) | `releases/` 不可变版本、`state/` 跨版本持久态、`install-manifest.json` |
| **我想把业务私有规则、MCP 工具与技能无污染挂载到本体** | [02. 业务知识叠加与服务编排](02-Business-Overlay-Guide.md) | `SOUL.generic.md` 裸标记 `{{BUSINESS_OVERLAY}}`、`profile_overlay.py`、物理技能落盘 |
| **我想了解后台研发专家岗位如何分工与通过路径所有权隔离** | [03. 研发专家协同与状态图引擎](03-Multi-Agent-and-StateGraph.md) | 8 大 `DEV_ROLES`、`RolePathOwnership` 强校验、`StateGraph` 检查点恢复与停滞熔断 |
| **我想排查卡片卡住或了解 `doctor` 报 `drift` 的根本归因** | [04. 交互卡片机制](04-Feishu-Cards-and-Delivery.md) 与 [05. 运维排障](05-Operations-and-Troubleshooting.md) | 卡片 6 态流转、单在途合并、`liveness.db` 活体对账、`stale_processes` 进程陈旧判定 |
| **我想掌握版本平滑升级、秒级回滚与历史版本安全清理** | [01. 部署指南](01-Quickstart-and-Install.md) 与 [05. 运维排障](05-Operations-and-Troubleshooting.md) | `superagent.py` 原生子命令（`update` / `rollback` / `prune-releases`）与原子软链切换 |

---

## 2. 60 秒亲手跑通核心闭环（零依赖真机实测）

以下实验脚本均基于仓库真实源码，无需拉取外部网络或启动后台服务，可在当前环境直接执行复现。

### 实验 B：任务池生命周期与围栏防御实测 (33 行代码)

运行以下脚本，验证任务提交幂等去重、租约过期被 Worker 2 接管后 Worker 1 迟到写入被围栏拒绝、以及 SQLite `jobs` / `outbox` 真实状态持久化：

```bash
python3 - <<'EOF'
import os, time, json
from pathlib import Path
from agentcore.pool import Pool

pool = Pool(Path('/tmp/sa_walkthrough_probe/pool'))
origin = {'profile': 'default', 'session': 's1', 'channel': 'feishu', 'chat': 'c1'}

# 1. 提交任务与重复提交幂等拦截
j1, dup1 = pool.submit('req-001', {'cmd': 'task'}, replay_safe=True, max_tries=2, origin=origin, return_meta=True)
j2, dup2 = pool.submit('req-001', {'cmd': 'task'}, replay_safe=True, max_tries=2, origin=origin, return_meta=True)
print(f'1. Submit: id={j1[:8]}... (dup1={dup1}, dup2={dup2})')

# 2. Worker 1 领单 (attempt=1) 后模拟崩溃/租约过期
c1, fd1 = pool.claim(lease=0.05)
os.close(fd1)
print(f'2. Attempt 1 claimed: token={c1["attempt"][:8]} tries={c1["tries"]}')
time.sleep(0.06)

# 3. Worker 2 (attempt=2) 接管；迟到的 attempt=1 写入被围栏拒绝
c2, fd2 = pool.claim(lease=0.05)
print(f'3. Attempt 2 claimed: token={c2["attempt"][:8]} tries={c2["tries"]}')
fenced = pool.finish(j1, c1['attempt'], ok=True, result={'out': 'worker1'})
print(f'   Attempt 1 finish result (fenced): {fenced}')

# 4. Worker 2 正常完工并查看 SQLite jobs / outbox 真实行数据
ok = pool.finish(j1, c2['attempt'], ok=True, result={'out': 'worker2_ok'})
os.close(fd2)
with pool.db() as db:
    job = dict(db.execute('SELECT id, status, tries, attempt, result FROM jobs WHERE id=?', (j1,)).fetchone())
    outbox = dict(db.execute('SELECT job, receipt FROM outbox WHERE job=?', (j1,)).fetchone())
print(f'4. Attempt 2 finish result: {ok}')
print(f'   SQLite jobs: {json.dumps(job, ensure_ascii=False)}')
print(f'   SQLite outbox: {json.dumps(outbox, ensure_ascii=False)}')
EOF
```

**真机捕获输出**：
```text
1. Submit: id=6c5f8b6b... (dup1=False, dup2=True)
2. Attempt 1 claimed: token=b0fce034 tries=1
3. Attempt 2 claimed: token=4f3e3998 tries=2
   Attempt 1 finish result (fenced): False
4. Attempt 2 finish result: True
   SQLite jobs: {"id": "6c5f8b6b950c40bd8a919506383ca931", "status": "completed", "tries": 2, "attempt": "4f3e39986960430293fe29587eaf237f", "result": "{\"out\": \"worker2_ok\"}"}
   SQLite outbox: {"job": "6c5f8b6b950c40bd8a919506383ca931", "receipt": "{\"job\": \"6c5f8b6b950c40bd8a919506383ca931\", \"status\": \"completed\", \"result\": {\"out\": \"worker2_ok\"}}"}
```

**临时失败重排补充机制**：当任务遭遇外部频控以 `exit_code=75` (`EX_TEMPFAIL`) 退出时，`finish(ok=False, result={'exit_code': 75})` 会触发 `tries` 从 1 自动回退至 0，状态置为 `queued`，心跳清空，不扣减业务重试配额：
```text
Claimed: tries=1, status=queued
After EX_TEMPFAIL (exit 75): status=queued, tries=0, heartbeat=None
```

---

### 实验 C：StateGraph 检查点恢复与停滞熔断实测 (25 行代码)

运行以下脚本，验证 `SqliteCheckpointer` 记录断点后自动跳过已完成节点 `n1`，直接从检查点恢复并流转 `n2`：

```bash
python3 - <<'EOF'
from pathlib import Path
from agentcore.state_graph import StateGraph, SqliteCheckpointer, Checkpoint, END

db = Path('/tmp/sa_walkthrough_probe/sg.db')
cp = SqliteCheckpointer(db)
task_id = 'task-probe-01'

builder = StateGraph()
exec_log = []
builder.add_node('n1', lambda s: (exec_log.append('n1'), {'v': 1})[1])
builder.add_node('n2', lambda s: (exec_log.append('n2'), {'v': s['v'] + 10})[1])
builder.set_entry_point('n1')
builder.add_edge('n1', 'n2')
builder.add_edge('n2', END)

# 模拟预置检查点：已在 n1 完成 checkpoint (v=100)
cp.put(Checkpoint(task_id=task_id, step=1, node='n1', state={'v': 100}))

graph = builder.compile(checkpointer=cp)
final = graph.invoke({}, config={'task_id': task_id})
latest = cp.get(task_id)

print(f'Executed nodes (n1 skipped): {exec_log}')
print(f'Final state: {final}')
print(f'Latest checkpoint: step={latest.step}, node={latest.node}, state={latest.state}')
EOF
```

**真机捕获输出**：
```text
Executed nodes (n1 skipped): ['n2']
Final state: {'v': 110, '_resumed_from_node': 'n1', '_resumed_from_step': 1}
Latest checkpoint: step=2, node=n2, state={'v': 110, '_resumed_from_node': 'n1', '_resumed_from_step': 1}
```

**停滞熔断守卫**：若智能体在循环节点中连续多次产生相同状态指纹，`stagnation_limit` 触发熔断：
```text
Caught StagnationDetectedError: StateGraph stagnation detected: node 'loop' repeated 3 times with zero state progress.
```

---

## 3. CLI 双层操作面与真实命令速查

Super Agent 区分 **Shell 包装器 (`superagent.sh`)** 与 **底层原生 CLI (`tools/superagent.py`)** 两层操作面：

1. **`install/superagent.sh` 包装语义**：
   - 支持子命令：`package`, `plan`, `install`, `update`, `doctor`, `uninstall`, `candidate-hermes`, `profile`；
   - **安全保护开关**：`install` / `update` / `uninstall` 若未带 `--apply`，仅执行计划预览（退出码 0）；带 `--apply` 时由脚本**剥离 `--apply` 后**透传调用底层；
   - 不支持 `rollback` 或 `prune-releases`（直接调用报 `unknown command`）。
2. **`python3 tools/superagent.py` 原生语义**：
   - 支持 12 个原生子命令：`plan`, `install`, `update`, `doctor`, `uninstall`, `activate-release`, `rollback`, `prune-releases`, `migrate-config`, `candidate-hermes`, `install-hermes-candidate`, `switch-gateway-source`；
   - **底层无 `--apply` 参数**：所有修改命令直接生效，追加 `--apply` 会直接报 `unrecognized arguments: --apply` 错误并退出。

| 运维场景 | 推荐命令 | 说明 |
| :--- | :--- | :--- |
| **安装计划预览** | `./install/superagent.sh plan --profile <p>` | 只读生成变更计划 JSON，零系统副作用 |
| **执行正式安装** | `./install/superagent.sh install --profile <p> --apply` | 构建不可变 release、建立 `current` 软链、配置插件 |
| **全局健康体检** | `python3 tools/superagent.py doctor --profile <p>` | 输出包含 17 项观测键的纯 JSON 诊断（退出码 0 为 ready） |
| **平滑版本升级** | `python3 tools/superagent.py update --profile <p>` | 构建新 release 并原子切换软链接，平滑接管未完成任务 |
| **秒级紧急回滚** | `python3 tools/superagent.py rollback --profile <p>` | 将 `current` 原子切回上一个稳定 release（无 `--apply`） |
| **历史版本清理** | `python3 tools/superagent.py prune-releases --profile <p> --keep 3` | 保留最近 3 个 release，安全回收磁盘空间（无 `--apply`） |
| **安全卸载清理** | `./install/superagent.sh uninstall --profile <p> --apply` | 停用受管服务并清理软链接，可选保留数据 |

---

## 4. 文档导航与技术规范

关于架构不变量、插件机制与生产环境配置的深入细节，请参考以下权威技术文档：
- [`docs/DEPLOY.md`](../DEPLOY.md)：单根拓扑规范、服务安装流程与发布锁定参数说明；
- [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md)：v2.0 架构全景深潜与 ADR-S1~S6 决策记录；
- [`docs/TECH_MANUAL.md`](../TECH_MANUAL.md)：系统技术手册与核心防御矩阵；
- [`docs/PLUGINS.md`](../PLUGINS.md)：14 个插件目录、`enabled` 门禁与挂载规范；
- [`docs/OPERATIONS.md`](../OPERATIONS.md)：布局感知运维指南与应急恢复流程。
