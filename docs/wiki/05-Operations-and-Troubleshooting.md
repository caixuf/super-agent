# 05. 运维体检与排障对账 (Operations, Doctor & Troubleshooting)

> **文档导航**：[Wiki 首页 (Home)](Home.md) │ [01. 单根受管部署与极速上手](01-Quickstart-and-Install.md) │ [02. 业务知识叠加与服务编排](02-Business-Overlay-Guide.md) │ [03. 研发专家协同与状态图引擎](03-Multi-Agent-and-StateGraph.md) │ [04. IM 交互卡片与交付机制](04-Feishu-Cards-and-Delivery.md) │ [05. 运维体检与排障对账](05-Operations-and-Troubleshooting.md)

---

## 1. 真实 `doctor` 健康体检与 17 项观测字典

### 1.1 判定逻辑与状态枚举
`tools/superagent.py doctor` 纯粹输出结构化 JSON 字典，不存在任何文本横幅。其顶层退出判定遵循三态逻辑：
- `"status": "ready"`（退出码 0）：发布完整、哈希一致、无陈旧进程且无任何漂移原因；
- `"status": "drift"`（退出码 1）：系统可用，但存在版本漂移、未重启陈旧进程（`stale_processes`）或脏工作区安装；
- `"status": "broken"`（退出码 1）：`current` 软链断裂、哈希校验失败或关键依赖缺失。

### 1.2 真实 JSON 结构片段
```json
{
  "status": "drift",
  "deploy_version_observation": {
    "status": "drift",
    "installed_commit": "22aff06a10ad9f73386c4e455a01c6f894bebb81",
    "installed_dirty": true,
    "source_repo": "/opt/src/super-agent",
    "stale_processes": [],
    "reasons": ["installed_dirty"]
  },
  "release_valid": true,
  "plugin_links_valid": true,
  "release_hash_valid": true,
  "runtime_observation": {
    "service": "hermes-gateway-probe.service",
    "service_state": "inactive"
  },
  "worker_observation": {
    "service": "superagent-worker-probe.service",
    "service_state": "inactive"
  },
  "worker_queue_observation": {
    "queues_observed": 2,
    "running_jobs": 0,
    "queued_jobs": 0,
    "healthy": true
  },
  "recall_diagnostics_observation": {
    "disk_bindings": { "profile_home": { "plugin": "matched" } }
  },
  "manifest": { ... }
}
```

---

## 2. 真实维护命令面与参数规范

运维工具严格区分原生命令行参数：

```bash
# 1. 历史版本安全清理（无预览模式，不接受 --apply，默认保留 3 个）
python3 tools/superagent.py prune-releases --profile <profile> --keep 3

# 2. 秒级紧急回滚（切回 manifest 记录的上一个 release，不接受 --apply）
python3 tools/superagent.py rollback --profile <profile>

# 3. 遗留服务纠偏与自动备份（备份至 state/drift-backups/）
bash scripts/ops/uninstall-legacy.sh
```

> [!NOTE]
> `uninstall-legacy.sh` 执行时若发现历史孤儿配置，会自动备份至 `${MANAGED_ROOT}/state/drift-backups/backup_<timestamp>/`，绝不静默物理删除。

---

## 3. 生产环境四大排障现场 (Troubleshooting)

### 现场 ①：`doctor` 体检报告 `"status": "drift"`
- **症状表现**：执行 `python3 tools/superagent.py doctor --profile <p>` 退出码为 1，状态标记为 `drift`。
- **诊断手段**：查看 JSON 输出中的 `deploy_version_observation.reasons`。
- **根因机制**：
  1. `stale_processes`：某个 Worker 或网关进程的 `/proc/{pid}/stat` 启动时钟早于当前 release 的 `installed_at` 时间戳，说明发版后对应服务未重启，仍运行旧代码；
  2. `installed_dirty`：当前线上 release 包含未提交的本地脏改动。
- **处置方案**：
  若为陈旧进程，重启对应服务即可：
  ```bash
  systemctl --user restart hermes-gateway-<profile>.service superagent-worker-<profile>.service
  ```
  若为脏安装，在源码仓提交代码后重新执行 `update`。

---

### 现场 ②：后台长任务长时间卡死在 `running` 状态
- **症状表现**：任务提交后卡在 `running`，卡片无进度刷新，无日志追加。
- **诊断手段**：
  1. 查询任务池：
     ```bash
     sqlite3 ~/super_agent/<profile>/state/analysis-queue/pool.db \
       "SELECT id, status, tries, heartbeat, attempt FROM jobs WHERE status='running';"
     ```
  2. 查询活体心跳：
     ```bash
     sqlite3 ~/super_agent/<profile>/state/liveness.db \
       "SELECT task_id, pid, host, state, phase, heartbeat_at FROM task_liveness WHERE state='running';"
     ```
- **根因机制**：Worker 进程崩溃或挂起。`agentcore/pool.py` 中的 `reconcile_watchdog()` 负责扫描超时任务，但当原 PID 在系统中依然存活（`/proc/{pid}/stat`）或其依然持有文件排他锁（`flock`）时，看门狗出于**防裂脑安全铁律**，坚决拒绝强行接管，防止旧 Worker 恢复后造成双重写入。
- **处置方案**：使用 `kill -0 <pid>` 确认进程是否假死；若假死，使用 `kill -9 <pid>` 强杀，看门狗在下一个检查周期（检测到进程消亡与锁释放）将安全触发重试。

---

### 现场 ③：外部限流 `EX_TEMPFAIL=75` 自动回退重排 vs 业务失败
- **症状表现**：任务未执行完，但数据库中状态退回 `queued`，且 `tries` 计数没有增加。
- **诊断手段**：
  ```bash
  sqlite3 ~/super_agent/<profile>/state/analysis-queue/pool.db \
    "SELECT id, status, tries, heartbeat, result FROM jobs WHERE id='<task_id>';"
  ```
- **根因机制**：Worker 调用大模型接口遭遇 HTTP 429 频控或临时断网，进程以退出码 75（`EX_TEMPFAIL`）退出。`pool.finish()` 判定 `exit_code == 75` 且 `replay_safe=True` 时，执行：
  `UPDATE jobs SET status='queued',heartbeat=NULL,tries=MAX(0, tries-1)`
  将 `tries` 自动回退并清空心跳，任务无损重排，不消耗珍贵的业务重试配额（`max_tries`）。
- **处置方案**：系统正常自愈行为，无需人工介入；若频繁发生，需在模型网关层排查并发限制。

---

### 现场 ④：长期记忆召回耗时高或命中率下降
- **症状表现**：智能体在处理包含上下文历史的会话时，首包响应延迟超过 500ms。
- **诊断手段**：
  ```bash
  # 审计脱敏指标
  python3 tools/inspect_recall_diagnostics.py --profile <profile>

  # 测量记忆数据库存储与常驻视图占用（只读，输出条数与字节数）
  python3 -m agentcore.memory_footprint --database <记忆库路径> --view-budget-mib 256
  ```
- **根因机制**：
  1. `state/memory-spool/` 目录下积累了大量未被索引消费的暂存事件；
  2. 退役事实（`status != 'active'`）的向量仍占据存储并被 `read_view` 拷入常驻副本，或 SQLite 向量表碎片化。
- **处置方案**：
  检查记忆服务运行状态：
  ```bash
  systemctl --user status superagent-memory-<profile>.service
  ```
  若服务正常且确认存在数据库碎片，可在停写窗口对目标记忆库执行整理：
  ```bash
  sqlite3 <记忆库路径> "VACUUM;"
  ```

---

## 4. 延伸参考
- 单根部署与服务安装：[01. 单根受管部署与极速上手](01-Quickstart-and-Install.md)
- 生产部署规范与发布锁定：[`docs/DEPLOY.md`](../DEPLOY.md)
- 布局感知运维脚本实现：[`docs/OPERATIONS.md`](../OPERATIONS.md)
