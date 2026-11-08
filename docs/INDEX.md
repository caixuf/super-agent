# Super Agent 技术文档导航索引（Master Index）

本文档为 Super Agent 技术体系的权威导航索引，汇总了系统架构、开发指南、生产部署、插件规范、运维对账与自愈演练全套技术文档。所有规范与实现严格遵循 **v2.0 单根架构（Single-Root Architecture）** 与 **全仓中立性门禁（Zero-Business Ratchet Gate）**。

---

## 1. 核心技术规范（必读总览）

| 规范文档 | 内容概要 | 目标读者 |
| :--- | :--- | :--- |
| [`TECH_MANUAL.md`](TECH_MANUAL.md) / [`SUPER_AGENT_TECHNICAL_MANUAL.md`](SUPER_AGENT_TECHNICAL_MANUAL.md) | **Super Agent 技术手册**：大模型原理、执行边界、持久任务池、并发与恢复语义、长期记忆、安全威胁模型、SLO 与故障处置矩阵。 | 架构师、全栈开发、SRE、安全审计 |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | **v2.0 架构全景深潜**：十二层分层架构、单根受管目录拓扑（`~/super_agent/<profile>`）、同事制多智能体协作、状态栅栏与因果确定性保证。 | 架构师、系统研发 |
| [`DEPLOY.md`](DEPLOY.md) | **生产部署与生命周期指南**：一键安装器（`superagent.sh`）、版本 Release、原子切换/回滚、身份暂存与业务叠加层挂载（`SOUL.generic.md`）、`hermes-current` 自动推断、容器干净室验证。 | DevOps、部署交付工程师 |
| [`PLUGINS.md`](PLUGINS.md) | **插件与工具面全景指南**：16 款受管插件（13 款通用内建 + 3 款受管外部）与 `localvec` 记忆提供方、两道启用门禁、同事 Profile 自动级联装配。 | 工具开发者、业务对接研发 |
| [`OPERATIONS.md`](OPERATIONS.md) | **布局感知运维与对账指南**：多布局感知引擎（`_layout.sh`）、遗留服务对账与平滑纠偏（`uninstall-legacy.sh`）、一键环境重构（`purge_and_rebuild_host.sh`）、`doctor` 诊断与召回观测。 | SRE、系统运维工程师 |

---

## 2. 先进 Agent 架构与工程实践指南（精读学习入口）

面向系统掌握常驻长效智能体（Always-on Production Agent）设计思想与工程底座的开发者：

- [`guide/README.md`](guide/README.md)：**指南总目录与伴读验证命令**
- [`guide/00_ARCHITECTURE_OVERVIEW.md`](guide/00_ARCHITECTURE_OVERVIEW.md)：**第 0 篇：全景导览 —— 从玩具 Agent 到生产级常驻智能体**
- [`guide/01_COLLEAGUE_AND_ORCHESTRATION.md`](guide/01_COLLEAGUE_AND_ORCHESTRATION.md)：**第 1 篇：多智能体组织学 —— 同事制、模型路由与工作区隔离**
- [`guide/02_DURABLE_RUNTIME_AND_LIVENESS.md`](guide/02_DURABLE_RUNTIME_AND_LIVENESS.md)：**第 2 篇：长任务运行时 —— 持久队列、插话抢占与主动探针**
- [`guide/03_MEMORY_ENGINEERING.md`](guide/03_MEMORY_ENGINEERING.md)：**第 3 篇：生产级长期记忆 —— 双路召回、代际硬隔离与容量治理**
- [`guide/04_EVIDENCE_AND_VERIFICATION.md`](guide/04_EVIDENCE_AND_VERIFICATION.md)：**第 4 篇：防幻觉与确定性工程 —— 证据门禁、流式屏障与干净室验证**

---

## 3. 核心机制活文档（跟着代码持续演进）

描述当前运行机制的核心工程约定：

| 文档 | 核心议题 | 权威实现位置 |
| :--- | :--- | :--- |
| [`MIGRATION.md`](MIGRATION.md) | 八层迁移清单 + 资产表：跨环境整仓拉起标准 | `install/`、`docker/` |
| [`POOL_RUNTIME.md`](POOL_RUNTIME.md) | 持久任务池：状态迁移、依赖锁、租约、结果 outbox 与调度后端 | `agentcore/pool.py`、`worker.py` |
| [`ATTEMPT_STOP_CONTRACT.md`](ATTEMPT_STOP_CONTRACT.md) | 启动许可、Subreaper 进程监管器停止回执与在途任务隔离 | `agentcore/attempt_supervisor.py` |
| [`DISPATCH_ANALYSIS.md`](DISPATCH_ANALYSIS.md) | 派工协议、执行计划门禁与只读分析追溯事实 | `agentcore/analysis_runner.py` |
| [`REDEPLOY.md`](REDEPLOY.md) | 重新部署、静态复验与工具注册核对步骤 | `tools/redeploy_check.py` |
| [`RELEASE_READINESS.md`](RELEASE_READINESS.md) | 发版发布核对清单、已验证修复与严格门禁 | `tests/`、`docker/verify.sh` |
| [`RELEASE_CAPABILITIES.md`](RELEASE_CAPABILITIES.md) | 源码能力面与发版核对：按模块分组标注证据层级与验收出口 | `agentcore/`、`plugins/` |
| [`FRONTDESK_BOUNDARY.md`](FRONTDESK_BOUNDARY.md) | 前台权限边界、可信上下文绑定与未授权拦截 | `patches/`、`runtime/` |
| [`MEMORY_IDENTITY_ROUTES.md`](MEMORY_IDENTITY_ROUTES.md) | 长期记忆个人身份路由、独立 Worker 隔离与撤销机制 | `agentcore/memory/` |
| [`MODEL_CONTEXT.md`](MODEL_CONTEXT.md) | 运行时动态探测模型上下文窗口，杜绝硬编码 | `agentcore/model_context.py` |

---

## 4. 专项设计规格与落地报告（Superpowers）

记录架构重大跃迁的设计初衷与实施闭环：

- **设计规格（Specs）**：
  - [`superpowers/specs/2026-11-07-single-writer-deployment-design.md`](superpowers/specs/2026-11-07-single-writer-deployment-design.md)：单一写者部署路径与能力默认值收敛设计。
  - [`superpowers/specs/2026-11-08-ops-scripts-layout-aware-design.md`](superpowers/specs/2026-11-08-ops-scripts-layout-aware-design.md)：官方运维脚本布局感知改造与真相源解析架构。
- **收尾报告（Reports）**：
  - [`superpowers/reports/2026-11-08-v2-convergence-and-docs-polish-report.md`](superpowers/reports/2026-11-08-v2-convergence-and-docs-polish-report.md)：v2.0 单根架构收敛、文档对齐与全仓验证交付报告。

---

## 5. 历史现场与验证报告（Historical Reports）

位于 `docs/reports/` 目录下，记录历史排查、断言数据与攻坚过程。
> **注意**：历史报告包含当时特定的现场测试数据与日志指纹，**仅用于追溯决策依据**；当前系统架构与运行时行为一律以最新代码和上述活文档为准。
