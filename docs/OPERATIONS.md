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
# 方式 1：指定受管 Profile 名称（自动读取 <managed_root>/state/recall-diagnostics/<profile_name>.json）
python3 tools/inspect_recall_diagnostics.py --profile <profile_name>

# 方式 2：显式指定源名称与绝对路径
python3 tools/inspect_recall_diagnostics.py --source <profile_name>=/path/to/<profile_name>.json
```

读取 `<managed_root>/state/recall-diagnostics/<profile_name>.json`，输出召回时延分位值、未命中样本与异常分布。

### 4.3 部署就绪与配置检查（`deployment_readiness.py`）

```bash
python3 tools/deployment_readiness.py --profile-home <profile_home> --profile <profile_name>
```

逐项验证：
1. 16 款受管插件与 `localvec` 记忆提供方的磁盘落位。
2. `config.yaml` 中各插件 `settings.enabled` 开关状态。
3. 关键依赖库与系统工具完整性。

### 4.4 干净 Clone 仓级复验门禁（`redeploy_check.py`）

```bash
python3 tools/redeploy_check.py --skip-docker
```

从零克隆验证：单元测试、安装器 dry-run、静态检查及环境完整性门禁。

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

---

## 6. 部署源锁定与防降级

为避免生产运维过程中的多仓并发更新、历史版本意外覆盖与未重启进程残留，运维体系引入部署源锁定与防降级门禁机制。

### 6.1 核心防护原则
1. **单一发布源绑定（One Deploy Source per Host）**：每个受管主机 Profile 绑定首次部署的源码真实路径（`source_repo`），避免多目录交叉部署导致状态紊乱。
2. **提交祖先防降级（Downgrade Prevention）**：升级时新提交必须是当前生效版本的 Git 祖先派生（`git merge-base --is-ancestor <prev> <new>`），防止旧代码静默覆盖生产环境。
3. **工作区干净度校验（Clean Tree Assertion）**：禁止直接将带有未提交修改的脏工作区打包发布为正式版本。
4. **多版本滚动保留（Release Retention）**：发布后保留当前激活版本及最近 N 个历史版本（默认 N=3），绝不清空历史记录，保障随时可秒级回滚。

### 6.2 拦截检查项（Blockers）与绕过参数
当命中国防门禁条件时，`install` 或 `update` 操作将直接阻断并输出 `{"status": "blocked", "blockers": [...]}`，系统状态零改动：

| 拦截标识 (Blocker) | 触发条件 | 绕过/覆盖参数 | 说明 |
| :--- | :--- | :--- | :--- |
| `source_dirty` | 当前发布源码树包含未提交的修改 | `--allow-dirty` | 允许发布包含未提交改动的临时版本 |
| `source_repo_mismatch` | 当前执行安装的源码仓库路径与历史清单不一致 | `--adopt-source` | 显式将发布源变更为当前仓库路径 |
| `downgrade_or_divergent` | 新提交不是已安装提交的 Git 后代分支（分支分叉或落后） | `--allow-downgrade` | 允许显式降级或强制覆盖 |
| `previous_commit_unknown_to_source` | 历史安装提交在当前仓库 Git 数据库中不存在 | `--allow-downgrade` | 允许跨非共享历史仓库部署 |
| `unexpected_commit` | 指定了 `--expect-commit <sha>` 但当前仓库 HEAD 提交不符 | 核对后重新指定匹配的 commit SHA | 防止运维操作发错提交 |

### 6.3 自动化运维与精准发布（`--expect-commit`）
运维发布脚本或流水线可通过 `--expect-commit` 显式锁死预期发版提交，防止分支切错：
```bash
./install/superagent.sh update --profile <profile> --expect-commit 4b825dc --apply
```

### 6.4 历史版本保留与清理策略
- 默认在 `install` / `update` 时自动保留激活版本及最近 3 个历史 Release。
- 历史清理命令：
  ```bash
  # 保留最近 3 个历史版本
  python3 tools/superagent.py prune-releases --profile <profile> --keep 3 --apply

  # 仅保留当前活跃版本，清理全部历史 Release
  python3 tools/superagent.py prune-releases --profile <profile> --keep 0 --apply
  ```
- 回滚命令（`superagent rollback --apply`）不受降级拦截限制，作为受控的标准回滚途径。

### 6.5 版本漂移对账（Doctor Drift）
`superagent doctor` 的 `deploy_version_observation` 节点对账当前运行态与源码态：
- 字段覆盖：`installed_commit`、`installed_dirty`、`source_repo`、`source_head`、`source_dirty`。
- `stale_processes`：检查已启动常驻进程的启动时间是否早于当前 Release 安装时间 `installed_at`。
- 若存在版本落后（`installed_behind_source`）、脏版本安装（`installed_dirty`）或未随新版本重启的常驻进程（`stale_processes`），`doctor` 状态判定为 `drift`（非 ready，退出码 1）。

