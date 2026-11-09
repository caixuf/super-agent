# 可信自主执行 M2、M3 与 M5 验证证据账本

- 记录日期：2026-11-09（Asia/Shanghai）
- 依据规范：[`TRUSTED_AUTONOMOUS_EXECUTION_PLAN.md`](../TRUSTED_AUTONOMOUS_EXECUTION_PLAN.md) 第 3 节（M2、M3、M5）与第 5 节交付规则
- 状态结论：**M2 验收回执绑定与工作树保护、M3 故障矩阵（全 8 场景）及 M5 受控技能生命周期与回滚演练已建立可重复测试证据并通过验证**。生产环境切换与无审批合并仍保持禁止。计划文档（`TRUSTED_AUTONOMOUS_EXECUTION_PLAN.md`）保持只读对账。

---

## 1. 负责人和范围

| 字段 | 内容 |
| :--- | :--- |
| **实施负责人** | Antigravity Autonomous Coding Agent (Subagent Worker) |
| **评审/验收责任人** | Super-Agent Orchestrator / Lead Security Reviewer |
| **授权测试范围** | 1. **M2 回执与补丁完整性**：隔离工作树验证回执绑定（`task`、`attempt`、代码快照哈希、策略摘要哈希）；代码变更后旧回执失效；跨任务与旧 attempt 冒用拦截；未收割工作树（脏改动/未合并提交）保留。<br>2. **M3 故障矩阵**（全 8 场景）：执行中取消取得停止证明后才确认终态；取消与成功并发仲裁；Worker/宿主重启核对原 attempt 防盲目重复执行；租约失效旧 attempt 迟到拒绝覆盖；队列级重复提交幂等契约；模型重复无效调用有界停机；外部动作响应丢失保留 `unknown` 拒绝对外盲重放；预算耗尽有界降级与人工流转。<br>3. **M5 受控经验复用与灰度**：案例绑定真实验收证据（默认 `approved=False`）；候选经验、审核通过与撤销技能三态流转；生产指引过滤未审核及已撤销技能；Profile/Subject/Team 访问隔离与越权拒绝；技能建议不可覆盖安全策略根目录或必验项；停止接单与版本回滚演练保持状态库、在途任务与未知投递完整。 |
| **禁止与未授权动作** | 严禁生产切换 (`superagent.py update/install`)、严禁重启生产守护进程、严禁自动合并到主分支、严禁未经审批向外部生产渠道发送真实交互、严禁自动晋升生产技能。 |

---

## 2. 输入身份

| 字段 | 标识与参数 |
| :--- | :--- |
| **源码基线 Commit** | `cf5020e4637651c6e68acfac155bfa83d543bf52`（含本地 M0/M1 与 M2/M3/M5 并行开发工作树） |
| **宿主环境** | Linux x86_64 WSL2 (Kernel 6.6.114.1-microsoft-standard) |
| **运行时** | Python 3.12.3 / pytest 9.1.0 |
| **测试隔离根目录** | `/tmp/pytest-of-*` 独立临时目录，与生产工作区完全隔离 |
| **策略与配置摘要** | 采用可信执行验证策略模型，严格遵循 `allowed_roots` 目录围栏与 `verification_checks` 必跑门禁 |
| **执行时间窗口** | 2026-11-09T09:00:00Z 至 2026-11-09T09:20:00Z |

---

## 3. 场景与结果（Scenarios and Results）

### 3.1 M2：回执与补丁完整性

| 测试用例 | 验证场景 | 结果 |
| :--- | :--- | :--- |
| `test_m2_receipt_binding_task_attempt_digests` | 验证回执强绑定 `task`、`attempt`、代码快照哈希与策略哈希 | **PASS** |
| `test_m2_reject_stale_receipt_after_code_modification` | 验证通过后代码若被修改，旧回执核验失败并报告代码快照不匹配 | **PASS** |
| `test_m2_reject_cross_task_or_stale_attempt_receipt_reuse` | 跨任务（`task-orig` -> `task-impostor`）或旧 attempt 复用回执被拦截 | **PASS** |
| `test_m2_preserve_unharvested_worktrees` | 存在脏改动或未合并分支提交时触发 `WorktreeStrandedError`，阻止工作树清理 | **PASS** |
| `test_m2_receipt_validation_edge_cases` | 工作树非目录校验、缺失代码哈希校验回执、策略哈希不匹配、非 approved 判定拦截及空目录哈希异常防御 | **PASS** |

### 3.2 M3：故障矩阵（全 8 场景）

| 矩阵项 | 场景说明 | 必须行为与实现契约 | 结果 |
| :---: | :--- | :--- | :--- |
| **Row 1** | 执行中取消 | 仅标记 `cancel=1`，进入 `canceling` 阶段；必须取得执行实例的真正停止证明后才确认终态 `cancelled`；未证明静止前拒绝终态并持久化 `unconfirmed` | **PASS** |
| **Row 2** | 取消与成功竞争 | 按 SQLite Pool 主账单事务仲裁；已标记取消的运行即使汇报成功，终态仍确认为 `cancelled`，杜绝矛盾终态 | **PASS** |
| **Row 3** | Worker/宿主重启 | 重启后 Worker 核对原 attempt（`is_current`）；旧或非法 attempt 无法结算；非租约到期不重复分配 | **PASS** |
| **Row 4** | 租约失效或旧 attempt 迟到 | 租约超时重新认领为 attempt 2 后，attempt 1 迟到的汇报被主账直接拒绝（`finish` 返回 False），无法覆盖新 attempt；旧回执拒绝被新 attempt 冒用 | **PASS** |
| **Row 5** | 重复提交 | 相同 `dedupe_key` 与相同参数幂等返回原 `job_id`；参数冲突时抛出拒绝异常；空 key 拒绝提交 | **PASS** |
| **Row 6** | 模型重复无效调用 | 连续 3 次相同或等价参数（含不同字典序参数）工具调用触发停机保护（`_detect_tool_stagnation`），有界停止 | **PASS** |
| **Row 7** | 外部动作响应丢失 | 外部投递超时或丢失后标记为 `unknown`；重新认领保持 `unknown`，杜绝盲目重放；租约超时自动进入 `unknown`；仅凭权威回执对账至 `delivered` | **PASS** |
| **Row 8** | 调用与资源预算耗尽 | `PlanGate` 耗尽 Pro 预算后有界降级至 Main；HITL 桥接流转人工审批；审批拒绝时记录原因并流转取消 | **PASS** |

### 3.3 M5：受控经验复用与灰度回滚

| 测试用例 | 验证场景 | 结果 |
| :--- | :--- | :--- |
| `test_m5_skill_forge_candidate_vs_approved_vs_revoked_lifecycle` | 案例默认 `approved=False`；无验证证据禁止批准；技能三态流转（候选/已批准/已撤销）；生产指引过滤未审核与已撤销技能 | **PASS** |
| `test_m5_profile_subject_team_isolation_and_post_revocation_denial` | 严格按 Profile、Subject、Team 隔离检索；跨主体/跨团队访问抛出 `PermissionError`；未授权无 profile 读请求硬拦截；撤销后访问拒绝 | **PASS** |
| `test_m5_case_isolation_and_evidence_lifecycle` | 案例检索与列表隔离（未授权及跨主体拒绝）；禁止基于未审核或无机器证据案例提炼/晋升正式技能 | **PASS** |
| `test_m5_atomic_storage_and_concurrent_consistency` | 案例与技能磁盘序列化采用 `.tmp` 原子替换，防御多进程并发写入/读取截断风险 | **PASS** |
| `test_m5_skill_advisory_cannot_override_policy_allowed_roots_or_verification` | 技能仅为参考输入，提示词中建议越权路径（如 `/etc`）被 `_workdir` 强制阻断；必测项不可被技能覆盖 | **PASS** |
| `test_m5_stop_intake_and_version_rollback_drill` | 停止接单（Stop-Intake）阻断新任务；版本从 v2.0 回滚到 v1.0 时，Pool SQLite 主账、在途运行任务、人工暂停任务及 `unknown` 外部投递记录完整保留且不盲重放 | **PASS** |

---

## 4. 原始执行命令与测试日志（Raw Verification Outputs）

### 4.1 专项回归测试
```bash
pytest -v tests/test_trusted_autonomous_execution_m3_m5.py
```
**输出日志**：
```text
============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-9.1.0, pluggy-1.6.0 -- /usr/bin/python3
cachedir: .pytest_cache
rootdir: /home/caixuf/code/super-agent
configfile: pyproject.toml
plugins: anyio-4.15.1, xdist-3.8.0
collecting ... collected 19 items

tests/test_trusted_autonomous_execution_m3_m5.py::test_m2_receipt_binding_task_attempt_digests PASSED [  5%]
tests/test_trusted_autonomous_execution_m3_m5.py::test_m2_reject_stale_receipt_after_code_modification PASSED [ 10%]
tests/test_trusted_autonomous_execution_m3_m5.py::test_m2_reject_cross_task_or_stale_attempt_receipt_reuse PASSED [ 15%]
tests/test_trusted_autonomous_execution_m3_m5.py::test_m2_preserve_unharvested_worktrees PASSED [ 21%]
tests/test_trusted_autonomous_execution_m3_m5.py::test_m2_receipt_validation_edge_cases PASSED [ 26%]
tests/test_trusted_autonomous_execution_m3_m5.py::test_m3_row1_cancel_during_execution_confirm_only_after_stop_proof PASSED [ 31%]
tests/test_trusted_autonomous_execution_m3_m5.py::test_m3_row2_cancel_vs_success_race_single_ledger_arbitration PASSED [ 36%]
tests/test_trusted_autonomous_execution_m3_m5.py::test_m3_row3_worker_restart_checks_original_attempt PASSED [ 42%]
tests/test_trusted_autonomous_execution_m3_m5.py::test_m3_row4_lease_expiry_stale_attempt_cannot_overwrite_new_attempt PASSED [ 47%]
tests/test_trusted_autonomous_execution_m3_m5.py::test_m3_row5_duplicate_submission_idempotency_contract PASSED [ 52%]
tests/test_trusted_autonomous_execution_m3_m5.py::test_m3_row6_repeated_identical_invalid_tool_calls_bounded_stagnation_stop PASSED [ 57%]
tests/test_trusted_autonomous_execution_m3_m5.py::test_m3_row7_external_action_response_lost_preserves_unknown PASSED [ 63%]
tests/test_trusted_autonomous_execution_m3_m5.py::test_m3_row8_budget_exhaustion_bounded_stop_and_human_escalation PASSED [ 68%]
tests/test_trusted_autonomous_execution_m3_m5.py::test_m5_skill_forge_candidate_vs_approved_vs_revoked_lifecycle PASSED [ 73%]
tests/test_trusted_autonomous_execution_m3_m5.py::test_m5_profile_subject_team_isolation_and_post_revocation_denial PASSED [ 78%]
tests/test_trusted_autonomous_execution_m3_m5.py::test_m5_case_isolation_and_evidence_lifecycle PASSED [ 84%]
tests/test_trusted_autonomous_execution_m3_m5.py::test_m5_atomic_storage_and_concurrent_consistency PASSED [ 89%]
tests/test_trusted_autonomous_execution_m3_m5.py::test_m5_skill_advisory_cannot_override_policy_allowed_roots_or_verification PASSED [ 94%]
tests/test_trusted_autonomous_execution_m3_m5.py::test_m5_stop_intake_and_version_rollback_drill PASSED [100%]

============================== 19 passed in 1.17s ==============================
```

### 4.2 关联模块联合无回归测试
```bash
pytest -q tests/test_skill_forge.py tests/test_verification_gate.py tests/test_trusted_autonomous_execution_m3_m5.py
```
**输出日志**：
```text
.............................................................            [100%]
61 passed in 1.81s
```

### 4.3 仓级业务与环境无关性门禁
```bash
pytest -q tests/test_repo_neutrality.py
```
**输出日志**：
```text
.................                                                        [100%]
17 passed in 1.09s
```

---

## 5. 交付证据与不可核实项（Evidence and Explicit Limitations）

### 5.1 已核实项
1. **回执防篡改与完整性核对**：验证回执在代码被修改或换用不同任务/attempt 时 100% 拒绝。
2. **状态机主账单仲裁**：并发取消与完成汇报、Worker 异常重启、租约过期重分配均由 SQLite 单主账保证无矛盾终态。
3. **外部副作用审慎处理**：投递状态丢失时保留 `unknown`，不触发不可逆动作的盲重试。
4. **技能全生命周期防御**：未审核案例不进生产指引，撤销后即刻断流，跨 Profile/Subject/Team 读请求硬拦截。
5. **回滚不丢在途状态**：服务版本回滚演练证明状态库、在途任务及未决投递数据零损坏、零重放。

### 5.2 不可核实项与局限性（Limitations）
1. **外部系统 Exactly-Once**：本实现保证了 Agent 侧主账与投递表的原子性与不盲重放，但不能单方面保证外部不可信三方系统的全局 Exactly-Once。
2. **真实生产网络环境**：本次测试在模拟隔离工作区及单元级测试桩中完成，真实生产物理网络隔离与凭据挂载需在后续发版部署中进一步核验。
3. **不可逆外部动作物理撤销**：代码回滚与进程停止无法物理撤销已经对外发送的消息或已写入外部系统的副作用；不可逆操作首版必须保持禁用或严格人工审批。

---

## 6. 结论与回退说明

- **结论**：M2、M3、M5 的核心契约已在 `agentcore/skill_forge.py` 与 `agentcore/verification_gate.py` 完成收口，并在 `tests/test_trusted_autonomous_execution_m3_m5.py` 中建立可重复的自动化回归防护，全部测试通过。
- **回退步骤**：
  - 如需回退本次变更，直接还原 `agentcore/skill_forge.py` 与 `agentcore/verification_gate.py`，并移除新增的测试用例文件 `tests/test_trusted_autonomous_execution_m3_m5.py`。
  - 数据存储层无不可逆 Schema 迁移，对现有业务与历史数据无兼容性破坏。
