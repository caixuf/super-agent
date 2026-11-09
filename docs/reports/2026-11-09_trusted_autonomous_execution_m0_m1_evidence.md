# 可信自主执行 M0 基线、M1 可信执行边界与 §4 第一批交付证据账本

- 记录日期：2026-11-09（Asia/Shanghai）
- 依据规范：[`TRUSTED_AUTONOMOUS_EXECUTION_PLAN.md`](../TRUSTED_AUTONOMOUS_EXECUTION_PLAN.md) 第 3、4、5 节
- 状态结论：**M0 基线、M1 任务级可信执行边界及 §4 第一批交付范围已完成代码实现与隔离实测验收；M2–M5 保持未勾选（`[ ]`），未做过度声明**。

---

## 1. 第 5 节必记六维证据总表

| 字段 | 内容 |
| :--- | :--- |
| **负责人和范围** | **实施负责人**：Antigravity Autonomous Coding Worker<br>**评审/验收责任人**：Super-Agent Coordinator / Lead Security Reviewer<br>**允许动作**：在独立测试 profile (`trusted_exec_test_profile`) 与独立 Git 测试仓库中完成 M0、M1 与 §4 第一批交付切片及正反例回归。<br>**禁止动作**：严禁生产切换 (`superagent.py update/install`)、严禁重启生产服务、严禁向生产队列/生产记忆写入或外发消息、严禁触碰外部并发会话持有的 `agentcore/memory/*` 等未提交文件。 |
| **输入身份** | **源码基线**：本地 `main`，父提交 `6006129`（含已提交 M0 改动 `20b6259`、`6006129`）。<br>**宿主环境**：Linux 5.15+ x86_64, Ubuntu 24.04 LTS, Python 3.12.3, `bubblewrap` (`/usr/bin/bwrap` v0.9.0)。<br>**冻结配置与隔离审计**：本工作流仅修改并暂存 `agentcore/attempt_supervisor.py`、`agentcore/colleague_containment.py`、`agentcore/colleague_worker.py`、`agentcore/dev_runner.py`、`tests/test_trusted_autonomous_execution_m0_m1.py` 及对应计划/报告文档；工作区中外部并发会话的 `agentcore/memory/*`、`plugins/localvec/*`、`plugins/memory_bridge/*`、`tools/recall_*`、`tests/test_memory_four_evolutions_and_bridge.py` 已核对并严格隔离，不纳入本次提交。 |
| **场景与结果** | **专项回归命令**：`pytest -v tests/test_trusted_autonomous_execution_m0_m1.py` → **`27 passed in 5.24s`**。<br>**全量关联与中性门禁命令**：`pytest -v tests/test_trusted_autonomous_execution_m0_m1.py tests/test_colleague_worker.py tests/test_colleague_containment.py tests/test_colleague_containment_trusted.py tests/test_colleague_dispatch.py tests/test_dev_runner.py tests/test_dev_exec_runner.py tests/test_colleague_dev_universal.py tests/test_cancel_request_truth.py tests/test_pool.py tests/test_repo_neutrality.py tests/test_business_neutrality.py` → **`238 passed, 1 skipped in 29.92s`**。<br>**跳过原因**：`tests/test_colleague_dispatch.py:82` 因当前 Python 环境未安装可选 C++ 扩展模块 `flowcoro_py` 而跳过 1 项，非关键门禁跳过。 |
| **执行证据** | 1. **宿主前置绑定 (`binding.json`)**：`attempt_supervisor.py` 与 `AttemptBoundSandboxAdapter.create_host_bound` 在 attempt 专属目录以 `0o600`、`O_CREAT \| O_EXCL \| O_NOFOLLOW` 写入 `binding.json`，绑定 `job`、`attempt`、`token`、`nonce`、`instance_id`、`generation`、`boot_id`、`supervisor_pid` 与 `supervisor_start`。<br>2. **双路径沙箱执行**：`dev_runner.py` 中模型工具执行与 `verification_gate` 验收检查均通过 `AttemptBoundSandboxAdapter` (`bwrap` + `PR_SET_CHILD_SUBREAPER`) 执行，产物 `report.json` 与 `manifest.json` 记录 `containment_binding`、`containment_audit_log` 与 `containment_evidence`。<br>3. **停止回执 (`stop.json`)**：证明普通子进程、`setsid`/`double-fork` 脱离会话子进程、超时进程及 Worker `SIGKILL` 崩溃后的全部后代进程均被收割（`quiescent: true`, `unquiesced_pids: []`）。 |
| **交付证据** | 第一批交付范围限于本地隔离测试 profile 与独立 Git 测试仓库中的补丁、产物 (`report.json`, `report.md`, `manifest.json`) 与队列回执核对；**不涉及真实外部前台通道投递**（真实通道投递属于 M4 范围，当前标记为未验收）。 |
| **结论和回退** | **结论**：M0、M1 及 §4 第一批交付范围全部通过实测验证；M2–M5 保持未完成/未验收。<br>**限制与阻断项**：真实模型端到端闭环（M2）、完整故障矩阵（M3）、真实前台会话投递（M4）与灰度晋升（M5）尚未实施。<br>**回退步骤**：若需回退本次 M1 变更，可执行 `git revert <commit_sha>` 恢复至 `6006129`，门禁将自动退回无适配器 100% 拒绝状态。 |

---

## 2. 真实已证明项与验证记录（Raw Counts & Commands）

### 2.1 M0 / M1 / §4 专项回归测试（27 项全通过）

```bash
pytest -v tests/test_trusted_autonomous_execution_m0_m1.py
```

- **原始结果**：`27 passed in 5.24s`
- **覆盖的具体正反例清单**：
  1. `test_m0_bridge_failure_does_not_silently_generate_synthetic_success` (PASS): 真实模式桥接不可用时抛出 `colleague_bridge_unavailable`，拒绝静默生成假成功。
  2. `test_m0_demo_mode_explicitly_labeled_as_synthetic` (PASS): 显式 `demo_mode=True` 输出、`manifest.json` 与 `report.json` 明确带有 `synthetic: true` 与 claim boundary。
  3. `test_m0_execute_in_worktree_does_not_commit_on_failure` (PASS): 研发失败保留工作树，严禁在 Git 分支创建假 commit。
  4. `test_m0_dev_runner_policy_require_verification_fails_closed_when_empty` (PASS): 政策要求必验时，缺检查项立即抛出 `required_verification_checks_missing`。
  5. `test_m0_dev_runner_payload_cannot_bypass_policy_require_verification` (PASS): Payload 尝试传入 `require_verification: false` 无法绕过政策约束。
  6. `test_m0_dev_runner_report_separates_execution_verification_and_artifacts` (PASS): 回执和报告独立区分 `execution_completed`, `verification_passed`, `artifacts_valid`。
  7. `test_m0_dev_runner_skills_recorded_as_unapproved_candidate` (PASS): 成功案例记录到 `SkillForge` 必须为 `approved: false`（候选经验，不自动晋升）。
  8. `test_section4_attempt_bound_supervisor_positive_file_edit_and_verification` (PASS): 单次运行在独立测试 profile 与独立 Git 测试仓库中受控修改文件并通过断言。
  9. `test_section4_attempt_bound_supervisor_stops_detached_descendants` (PASS): `attempt_supervisor` 递归清理 `setsid` 孤儿后代进程。
  10. `test_section4_tampered_or_invalid_proof_rejected` (PASS): 篡改 Token/Job/Attempt 或未静默回执被 `verify_subreaper_containment` 拦截。
  11. `test_m0_execute_in_worktree_does_not_commit_on_synthetic_demo_mode` (PASS): 合成 demo 执行不向 Git 提交代码。
  12. `test_m0_demo_mode_env_var_falsy_values_disable_demo_mode` (PASS): `AGENTCORE_DEMO_MODE=0/false/no` 准确判定为禁用。
  13. `test_m0_dev_runner_report_execution_failed_marks_execution_completed_false` (PASS): 执行失败时 `execution_completed: false`。
  14. `test_section4_verify_subreaper_containment_rejects_missing_identity_fields` (PASS): 缺少 `job`/`attempt`/`nonce`/`supervisor_pid`/`exit_code` 的回执全部拒绝。
  15. `test_m1_gate_refuses_self_declared_bool_runner` (PASS): 自报布尔值的 Runner（`has_trusted_containment_proof() -> True` 或 `trusted_containment = True`）被门禁拒绝。
  16. `test_m1_gate_refuses_when_no_backend_adapter` (PASS): 无后端适配器时，事前门禁返回 False（Fail-Closed）。
  17. `test_m1_gate_refuses_well_formed_stop_proof_alone` (PASS): 合法有效的 `stop.json` 仅证明 attempt 已停止，绝不能开启事前执行门禁。
  18. `test_m1_proof_from_different_attempt_job_or_nonce_is_refused` (PASS): 跨 job/attempt/nonce/token 的绑定或无 `bwrap_subreaper` 证明的伪适配器严格拒绝；仅四项身份全匹配的真实 `AttemptBoundSandboxAdapter` 放行。
  19. `test_colleague_worker_refuses_fake_supervisor_proof_file` (PASS): 环境变量指向伪造证明文件时被门禁直接拦截。
  20. `test_m1_host_minted_binding_rejects_tampering_symlinks_and_dead_supervisor` (PASS): 校验 `binding.json` 的 `instance_id`、`generation`、`boot_id`；拒绝权限过宽（`0o666`）、符号链接替换及已退出 supervisor PID 的绑定。
  21. `test_m1_payload_self_declared_binding_fields_rejected` (PASS): 任务 `payload` 自报 `trusted_containment` 或 `instance_id` 时在执行前直接抛出 `PermissionError("payload_binding_self_declaration_forbidden")`。
  22. `test_m1_gateway_delegated_and_host_owned_runners_refused_even_with_real_adapter` (PASS): `gateway_delegated` 与 `host_owned` 执行器即使挂载真实适配器也一律被拒绝。
  23. `test_m1_pre_execution_and_kernel_bwrap_enforcement_of_paths_credentials_network_and_tools` (PASS): 同时验证执行前检查与 `bwrap` 内核级强制隔离——非白名单工具拒绝、未授权网络拒绝、`~/.ssh`/`~/.config`/`~/.hermes`/`/var/run/docker.sock` 拒绝、岗位 `never` 路径（`SPEC.md`、`.git/config`）在沙箱内写入返回 `EROFS (errno 30)`、工作树外宿主文件不可见、沙箱内连接宿主回环 TCP 端口被 `--unshare-all` 阻断。
  24. `test_m1_resource_budgets_enforced_and_invalid_policy_fails_closed` (PASS): 验证无效预算策略直接 Fail-Closed 报错，且 `timeout_s`、`max_output_bytes`、`max_memory_bytes` (`RLIMIT_AS`)、`max_procs` (`RLIMIT_NPROC`)、`max_file_bytes` (`RLIMIT_FSIZE`) 与 `max_disk_bytes`（含超限自动回滚工作树修改）全部实测生效。
  25. `test_m1_cancel_blocks_new_actions_and_reaps_setsid_descendants` (PASS): 两阶段 `cancel()` 先阻断后续执行请求，再终止并收割普通及 `setsid` double-fork 脱离会话子进程（宿主 `os.kill(pid, 0)` 确证 `ProcessLookupError`）；身份不符时保留 `status="unknown"`, `quiescent=False`。
  26. `test_m1_worker_sigkill_crash_reaps_sandbox_and_setsid_descendants` (PASS): 对真实 Worker 进程发送 `SIGKILL` (`kill -9`) 后，沙箱 subreaper 立即检测控制套接字 `EOF` 并连根收割 `bwrap` 与 `setsid` 脱离会话孙进程，确认无任何残留进程。
  27. `test_m1_end_to_end_dev_runner_and_pool_with_attempt_bound_sandbox_adapter` (PASS): 验证 `run_dev_job`（模型工具修改 + `verification_gate` 独立测试均在 `AttemptBoundSandboxAdapter` 内运行）及 `ColleagueDispatch` -> `Pool.run_claimed` -> `attempt_supervisor` -> `colleague_worker` (`AttemptBoundSandboxAdapter.from_environment`) 全链路通过，且 `binding.json` 与 `stop.json` 的 `instance_id`、`generation` 一致。

---

## 3. 计划文档状态对账

核对 [`docs/TRUSTED_AUTONOMOUS_EXECUTION_PLAN.md`](../TRUSTED_AUTONOMOUS_EXECUTION_PLAN.md)：
- **M0 工作项（第 63–67 行）**：已全部完成并由测试与基线账本覆盖，更新为 `[x]`。
- **M1 工作项（第 82–88 行）**：已全部完成并由 `AttemptBoundSandboxAdapter` 实测覆盖，更新为 `[x]`。
- **§4 第一批工作项（第 177–181 行）**：已全部完成并由正反例回归覆盖，更新为 `[x]`。
- **M2、M3、M4、M5 工作项**：属于后续批次范围，保持 `[ ]` 未勾选。
