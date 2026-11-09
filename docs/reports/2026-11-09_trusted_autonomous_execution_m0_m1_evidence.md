# 可信自主执行 M0/M1 第一批交付证据账本

- 记录日期：2026-11-09（Asia/Shanghai）
- 依据规范：[`TRUSTED_AUTONOMOUS_EXECUTION_PLAN.md`](../TRUSTED_AUTONOMOUS_EXECUTION_PLAN.md) 第 4、5 节
- 当前状态：**第一批交付（M0 基线与 M1 最小切片）已验证完成**。生产部署、真实外部渠道与自动合并部署保持禁用并等待授权。

---

## 1. 负责人和范围

| 字段 | 内容 |
| :--- | :--- |
| **实施负责人** | Antigravity Autonomous Coding Agent |
| **评审/验收责任人** | Super-Agent Administrator / Lead Verification Reviewer |
| **允许动作** | 本地测试仓库代码受控修改、独立测试 Profile 运行、Attempt Supervisor 进程树生命周期与停止证明核验、单元与集成回归验证、提交和推送代码与测试。 |
| **禁止动作** | 严禁生产切换 (`superagent.py update/install`)、严禁重启生产服务、严禁向 `caixuf`/`<business-domain>` 生产 Profile 执行 `sync_roles.py --apply`、严禁外发真实 IM/平台消息、严禁写入生产记忆、严禁自动合并或部署生产。 |

---

## 2. 输入身份

| 字段 | 内容 |
| :--- | :--- |
| **源码基线** | 本地 `main` 分支，提交基线 `5050d74`（HEAD） |
| **宿主环境** | Linux x86_64, Kernel 5.15+, Python 3.12.3, pytest 9.1.0, PR_SET_CHILD_SUBREAPER 支持 |
| **策略与配置** | 独立测试策略：`require_verification: true`，`allowed_roots: [isolated_test_repo]`，受限工具集与工作树锁 |
| **环境与时间窗口** | 独立隔离目录（`/tmp/pytest-of-caixuf/...` 与本地独立临时目录），时间窗口：2026-11-09 15:40 ~ 16:00 CST |

---

## 3. 场景与结果

| 场景编号 | 场景分类 | 场景描述与执行命令 | 预期结果 | 实际结果 | 状态 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **SC-01** | M0 正向 | 显式启用 `demo_mode` 时，执行输出、`manifest.json` 与 `report.json` 必须显式标记 `synthetic: true` 与声明边界 | 标明合成，不可充当真实证据 | 正确标记 `synthetic: true` 与 claim_boundary | **PASS** |
| **SC-02** | M0 故障反例 | 真实模式下，平台桥接不可用时禁止静默退化为模拟成功 | 明确失败，`ok=False`，报 `colleague_bridge_unavailable` | 抛出明确失败，未产生假成功 | **PASS** |
| **SC-03** | M0 故障反例 | `execute_in_worktree` 在研发失败时严禁创建虚假成功 Git commit | 失败保留工作树，不提交 commit | 保留失败工作树，分支无提交记录 | **PASS** |
| **SC-04** | M0 故障反例 | 策略配置 `require_verification: true` 时，缺少必跑检查的任务必须在执行前被拒绝 | 抛出 `required_verification_checks_missing` | 准确拦截，拒绝执行 | **PASS** |
| **SC-05** | M0 故障反例 | Job payload 尝试传入 `require_verification: false` 试图绕过策略要求 | 策略优先，拒绝绕过 | 抛出 `required_verification_checks_missing` | **PASS** |
| **SC-06** | M0 正向 | 成功执行报告必须独立分离执行状态、验收通过状态与产物有效性 | `execution_completed`, `verification_passed`, `artifacts_valid` 独立字段 | 字段完整记录且均为 True | **PASS** |
| **SC-07** | M0 边界 | 执行成功沉淀案例到 `SkillForge` 时，必须标记为未审批候选经验，禁止自动晋升为生产技能 | `approved: false` | 案例持久化 `approved: false` | **PASS** |
| **SC-08** | M1/Sec4 正向 | 在独立测试 Profile 与独立测试仓库中，Attempt Supervisor 监管下完成代码修改并通关验证 | 修复 `calc.py` 漏洞，`test_calc.py` 退出码 0，生成 `stop.json` | 修改成功，测试通过，静默停止证明完备 | **PASS** |
| **SC-09** | M1/Sec4 停止 | Attempt 终止/超时时，`attempt_supervisor` 递归清理所有脱离会话的孤儿后代进程 | 杀死 `setsid` 孤儿进程，`waitpid` 达到 ECHILD，`quiescent: true` | 后代 PID 彻底销毁，证据写入 `stop.json` | **PASS** |
| **SC-10** | M1/Sec4 故障反例 | 伪造 Token、复用 Job、篡改 Attempt、残留未清理 PID 或单纯布尔值断言 | `verify_subreaper_containment` 逐项拒绝并标明根因 | 精确拦截：`proof_token_mismatch`, `proof_job_mismatch`, `unquiesced_descendants_remain` | **PASS** |
| **SC-11** | M0 故障反例 | 显式 `demo_mode` 下的合成执行严禁向 Git 分支创建假提交，且必须保留 Worktree | 不向 Git 分支提交，`worktree_preserved: true` | 正确阻止提交，完整保留工作树 | **PASS** |
| **SC-12** | M0 边界 | 环境变量 `AGENTCORE_DEMO_MODE=0/false/no` 等假值严禁被误判为启用 Demo 模式 | 桥接缺失时保持关闭并报错 `colleague_bridge_unavailable` | 准确识别假值，严密阻断静默合成模式 | **PASS** |
| **SC-13** | M0 故障反例 | Runner 执行失败时，收口报告与回执中 `execution_completed` 必须为 False | 报告与回执明确标记 `execution_completed: false` 与 `ok: false` | 准确标记，未将失败标记为执行完成 | **PASS** |
| **SC-14** | M1/Sec4 故障反例 | 缺失 `job`/`attempt`/`nonce`/`supervisor_pid`/`exit_code` 的不完整证明核验 | `verify_subreaper_containment` 严格拒绝，`evidence_id` 返回 None | 严格拦截缺字段证明，消除未绑定证据隐患 | **PASS** |
| **SC-15** | M1/Sec4 故障反例 | 环境变量 `AGENTCORE_SUPERVISOR_PROOF_PATH` 指向伪造或无效文件时尝试穿透门禁 | `_trusted_containment_available` 验证证明内容并拒绝 | 准确拦截伪造文件，未绕过前置门禁 | **PASS** |

---

## 4. 执行证据

- **测试套件**：`tests/test_trusted_autonomous_execution_m0_m1.py`（15 项专项验证，15 passed in 2.53s）。
- **定向关联回归**：`242 passed, 2 skipped in 24.88s`（覆盖全部受改动模块依赖、新测试 `test_trusted_autonomous_execution_m0_m1.py` 以及 `test_repo_neutrality.py` 等关联测试文件；按编排规范全库全量回归在各 Agent 交付后由调度编排器统一执行）。
- **静默停止证据摘要**：
  ```json
  {
    "proof_scope": "subreaper_tree",
    "quiescent": true,
    "exit_code": 0,
    "unquiesced_pids": [],
    "child_started": true,
    "evidence_id": "<sha256:32-char-digest>"
  }
  ```

---

## 5. 交付证据与后续里程碑阻断项

1. **当前已交付（第一批）**：
   - `colleague_dev.py`：消除隐式假成功，合成兜底收口至显式 `demo_mode`，失败不向 Git 提交。
   - `dev_runner.py`：强制政策级必需验收项，分离执行/验收/产物三态，经验仅作候选（`approved=False`）。
   - `colleague_containment.py`：提供 `verify_subreaper_containment` 与专属 `evidence_id`，打通 attempt 专属进程树停止证明。
   - `colleague_worker.py`：对接 `attempt_supervisor` 证据验证，拒绝凭空信任布尔标记。
2. **后续里程碑所需外部授权与阻断项（按需上报）**：
   - **M2（真实模型研发闭环）**：需要宿主配置活跃的模型 API Key 与受权真实业务仓库检出。
   - **M3（异常与故障恢复）**：需要执行崩溃故障注入与租约迟到重放演练。
   - **M4（真实前台交付）**：需要明确授权测试 Profile、测试群聊 ID (`FEISHU_TEST_CHAT_ID`) 与应用凭据 (`FEISHU_APP_ID`, `FEISHU_APP_SECRET`)。禁止在未经授权时向真实群发消息。
   - **M5（生产经验晋升与灰度）**：需要管理员人工审批流介入，以确定经验是否可转化为生产技能。

---

## 6. 结论和回退

- **结论**：**通过（PASS）**。第一批（Section 4 全部 5 项与 M0/M1 对应项）验收全部通过，证据真实完整。
- **回退步骤**：若需回滚，直接 `git restore agentcore/ tests/` 即可安全还原，未对生产运行库和生产系统产生任何外部副作用。
