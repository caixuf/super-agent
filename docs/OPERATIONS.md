# Super Agent 布局感知运维与对账指南（Operations & Drift Reconciliation）

本文档为 Super Agent 的日常运维巡检、配置漂移对账、故障排查与自愈演练操作指南。

---

## 1. 布局感知架构与真相源解析（`scripts/ops/_layout.sh`）

在生产环境中，智能体可能运行于不同的拓扑布局（例如单根受管布局 `~/super_agent/<profile>` 或单一写者布局 `~/.hermes/agentcore`）。为避免运维脚本假设单一拓扑而误报或罢工，本仓运维脚本库统一引入 **布局感知引擎（Layout Resolver）**。

### 1.1 真相源解析优先级

运维脚本解析引擎路径（`ENGINE_ROOT`）时，严格遵循纯函数推断顺序：

```
1. 环境变量 AGENTCORE_ENGINE_ROOT（显式覆盖）  --> 布局: explicit
2. ${AGENTCORE_ROOT}/<profile>/current 存在     --> 布局: managed（单根受管）
3. ${HERMES_HOME}/agentcore 存在               --> 布局: install.sh（单一写者）
4. 均不存在                                    --> 拒绝执行（exit 1），保护系统状态
```

此设计保证了：
- **安全不放宽**：真相源不存在时绝对不盲目执行清理。
- **自动对齐**：针对不同布局，备份范围、清理对象与验证命令自动切换，无需手工改写脚本。

---

## 2. 遗留配置清理与漂移对账（`scripts/ops/uninstall-legacy.sh`）

### 2.1 适用场景
- 检查系统是否存在历史孤儿服务单元（如废弃的网关、旧版 sidecar 单元）。
- 探测 systemd 模板单元（`agentcore-worker@.service`、`agentcore-memory@.service`）的 `PYTHONPATH` 是否漂移至非发布目录。
- 探测 Profile 配置是否存在多写者二义性。

### 2.2 操作步骤

```bash
# 步骤 1：只读演练（Dry-run），输出对账报告
bash scripts/ops/uninstall-legacy.sh

# 步骤 2：确认无误后执行纠偏与清理（安全备份至 drift-backups/）
bash scripts/ops/uninstall-legacy.sh --apply
```

### 2.3 安全回退保障
在纠偏任何 systemd 单元文件或配置文件前，脚本自动将原始文件复制至：
`${HERMES_HOME}/drift-backups/unit-drift-<timestamp>/`
若需要回退，只需原样拷回并执行 `systemctl --user daemon-reload`。

---

## 3. 主机一键环境重构（`scripts/ops/purge_and_rebuild_host.sh`）

### 3.1 适用场景
当主机环境遭受严重污染、不可逆依赖损坏或需复现生产干净环境时，执行一键环境重建。

### 3.2 运行机制
1. **自动冻结与快照**：完整备份现有 Profile 的 `config.yaml`、记忆数据库与状态账本至私有归档目录。
2. **安全停服与清理**：停止相关 user 级别 systemd 单元，解除旧文件锁。
3. **官方安装器重建**：调用官方受管安装器重新构建环境，绝不手写路径拼装代码。
4. **全套门禁自验**：重建完成后自动执行就绪验证，输出验收结果。

```bash
# 预览重构计划
bash scripts/ops/purge_and_rebuild_host.sh

# 确认后执行重构
bash scripts/ops/purge_and_rebuild_host.sh --apply
```

---

## 4. 日常健康体检与诊断观测工具面

### 4.1 全局健康体检（`superagent doctor`）

```bash
./install/superagent.sh doctor --profile <profile_name>
```

输出关键检查项：
- `manifest`：安装版本指纹与不可变 Release SHA256 对账。
- `disk_bindings`：主 Profile 与所有研发同事 Profile 的插件及引擎软链一致性状态。
- `recall_diagnostics_observation`：向量检索耗时、Top-K 相似度分布与命中率统计。
- `worker_observation`：持久任务队列租约、在途任务代次与 Worker 进程存活。

### 4.2 独立召回诊断观测器（`inspect_recall_diagnostics.py`）

用于持续分析长期记忆向量检索的性能与健康水线：

```bash
python3 tools/inspect_recall_diagnostics.py --profile <profile_name>
```

读取 `<managed_root>/state/recall-diagnostics/<profile_name>.json`，输出召回时延分位值、未命中样本与异常分布。

### 4.3 部署就绪门禁（`redeploy_check.py`）

```bash
python3 tools/redeploy_check.py --profile-home <profile_home> --profile <profile_name>
```

逐项验证：
1. 16 款受管插件与 `localvec` 记忆提供方的磁盘落位。
2. `config.yaml` 中各插件 `settings.enabled` 开关状态。
3. 关键依赖库与系统工具完整性。

---

## 5. 常驻服务单元与看门狗治理

| 服务单元 | 职责定位 | 异常处置原则 |
| :--- | :--- | :--- |
| `hermes-gateway-<profile>.service` | 消息接入网关与交互路由 | 崩溃时由 systemd 自动拉起；若遇配置损坏进入失败态，检查日志 |
| `superagent-worker-<profile>.service` | 持久任务 Worker 执行器 | 具备 Attempt 围栏保护；重启后仅领单未终态任务，不发生脏写 |
| `superagent-memory-<profile>.service` | 记忆维护与后台索引巩固 | 独立于主会话；遇 SQLite 锁争用自动重试 |

日志巡检常用命令：
```bash
journalctl --user -u hermes-gateway-<profile>.service -n 100 -f
journalctl --user -u superagent-worker-<profile>.service -n 100 -f
```
