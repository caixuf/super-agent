# 后台完成回执：投递、体检与存量积压处置（SOP）

这篇只管一件事：**队列里的完成回执有没有真的回到它当初出发的那个会话**。
配合 `plugins/durable_dispatch/README.md`（插件设置）与 `docs/POOL_RUNTIME.md`（持久任务池）读。

## 0. 三条不可让步的判据

1. **可投递性只来自提交时写下的路由**：`jobs.origin` 里必须有 `route`（且 `profile` 与当前进程一致）。
   **没有任何"退回当前活跃会话"的兜底** —— 一条回执落在哪个聊天，必须是当初提交它的那个回合决定的。
2. **不可投递就 park，不猜、不 ACK**：无 `route` 的（历史遗留）或属于别的 profile 的回执留在 `outbox`
   未 ACK，被跳过而不阻塞后面的可投递项。
3. **只有拿到传输层回执才算投递成功**：`outbox.acknowledged=1` + `receipt_id` 才叫投出去了。
   `delivered` 计数与"没报错"都不算。

## 1. 体检（只读，升级后与排障时先跑）

```bash
ENGINE=<agentcore checkout>            # 例如 ~/code/super-agent
PYTHONPATH=$ENGINE python3 -m agentcore.completion_backfill \
  --root <queue_root> --profile <profile> --limit 50
```

判读：

| 输出 | 含义 | 动作 |
| :--- | :--- | :--- |
| `deliverable: true` | 有路由、能投出去 | 什么都不做；poller 会投（见 §3） |
| `deliverable: false` | **永远不会自动投递**：无 `route` 或 profile 不匹配 | 走 §2 或判为永久 park |
| `delivered` 一遍遍为 0 而 `parked` 不为空 | 队列头部被无主回执占住（按设计跳过，但没投） | 走 §2 |

`queue_root` 不止一个：`plugins.entries.durable-dispatch.settings.queue_root`（主 Bot / analysis）
**以及每个 colleague 的 `queue_root`**。枚举一次全跑：

```python
import os, subprocess, sys, yaml
home = os.path.expanduser(sys.argv[1])          # profile home
engine = sys.argv[2]                            # agentcore checkout
cfg = yaml.safe_load(open(os.path.join(home, 'config.yaml')))
settings = cfg['plugins']['entries']['durable-dispatch']['settings']
roots = [settings['queue_root']] + [c['queue_root'] for c in (settings.get('colleagues') or {}).values()]
env = dict(os.environ, PYTHONPATH=engine)
for root in roots:
    print('==', root)
    subprocess.run([sys.executable, '-m', 'agentcore.completion_backfill',
                    '--root', root, '--profile', 'PROFILE'], env=env)
```

## 2. 存量无主回执：管理性修复（`Pool.attach_origin`）

**这是运维动作，不是自动动作。** 引擎故意不替你猜路由，只提供带证明的恢复 API。

前置条件（全部满足才受理，任一不满足即拒绝）：

- `jobs.origin IS NULL`，且 job 已是终态（`completed` / `failed` / `cancelled`）；
- 回执**未被碰过**：`acknowledged=0`、无 `receipt_id`、无 `delivered`、无在租的 `lease_token`；
- `expected_payload_sha256` 与库里 `payload` 的 sha256 **逐字节一致**（证明你核的是这一条请求）。

步骤：

```python
import hashlib, sqlite3, json, sys
sys.path.insert(0, '<agentcore checkout>')
from agentcore.pool import Pool

root, job = '<queue_root>', '<32-hex job id>'
pool = Pool(root)
with pool.db() as db:
    payload = db.execute('SELECT payload FROM jobs WHERE id=?', (job,)).fetchone()['payload']
print('payload_sha256 =', hashlib.sha256(payload.encode()).hexdigest())

pool.attach_origin(
    job,
    origin={'profile': '<profile>', 'session': '<原会话>', 'route': 'agent:<profile>:<platform>:dm:<chat>',
            'channel': '<platform>', 'chat': '<chat id>'},
    expected_payload_sha256='<上一步的 sha256>',
    proof_id='<你的取证人/单据号，1..128 字符>',
    proof_sha256='<该取证物料的 sha256（64 位 hex）>',
)
```

约束与后果：

- `route` 必须来自**提交回合的证据**（原始会话标识、平台回执、取证人签核）。凭"大概是那个群"推断就是伪造归属。
- 该调用只写 `origin_repairs` 证明账本 + 仅当 `origin IS NULL` 时更新 `jobs.origin`，**不** ACK、不改 payload/结果、不重投。
- 重放同一条证明是幂等的；与已记录证明冲突会报错，不会被覆盖。
- 修完**再跑一遍 §1**：出现 `deliverable: true` 即可由 poller 投递。
- 恢复不了（路由无从证明）就让它保持 park，并在巡检记录里留档；**不要**为了"看起来干净"删回执或编一个路由。

## 3. 投递是谁在做

`deliver_completions: true` 时，插件起一条后台线程扫 `outbox`（间隔 `delivery_interval`，默认 5s），
用 `delivery_transport_file` 指定的传输器投递；投递成功写入传输回执并 ACK。
重启、重连、换进程都不改变结果：每一趟都从 ledger 重新读，已投递的不重放，未投递的继续。
不确定的发送被 fence 成待查（不会当成功），可投递的才重试。

## 4. 投递健康（积压不再静默）

每趟扫完都会记一条**持久健康观察**（`report_delivery_health`，默认 true），scope =
`durable-completion-delivery`：

- `delivery_health_fault_grace`（默认 60s）：积压持续这么久 → 入一条 `health_fault` 事件；
- 积压清空并稳定 10s → 入一条 `health_recovery` 事件；
- 观察只带计数与结果**名称**（`parked` / `outcomes`），**不含**路由、job id、任务文本或路径。

事件落在**共享 ops-alert outbox**（就在同一个 `pool.db` 的 `ops_alert_outbox` 表）。要真正送达，
宿主必须跑发送侧：`agentcore.ops_alert_senders`（webhook）+ `agentcore.ops_alert_daemon`
（或宿主自建 `Sender`）——本仓默认不构造、不启动。**没有发送器时事件会堆在库里没人看**，
所以插件同时对"积压摘要发生变化"各打一条 WARNING（`agent.log` 可查）。

## 5. 现象对照表

| 现象 | 判据 | 动作 |
| :--- | :--- | :--- |
| 「派了工，结尾只留在队列库，聊天里看不到」 | §1 里该回执 `deliverable:false` | §2 修复或判为永久 park |
| 反复重投、聊天里重复出现 | 传输器确实投了但 ACK 失败（at-least-once 兼容路径） | 改用直投传输器（真实平台回执 + fence） |
| 每次 `drain` 都 `delivered:0` | 头部是无主回执（按设计跳过） | §1 找到 `deliverable:false` 的条目 |
| 升级后"同事像没干活" | 工具面里没有 `durable_submit`（插件整块加载失败） | 先修插件加载（见 `docs/PLUGINS.md`），再看 §1 |