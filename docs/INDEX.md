# Super Agent 技术文档导航索引（Master Index）

本文档为 Super Agent 技术体系的权威导航索引，汇总了系统架构、开发指南、生产部署、插件规范、运维对账与自愈演练全套技术文档。所有规范与实现严格遵循 **v2.0 单根架构（Single-Root Architecture）** 与 **全仓中立性门禁（Zero-Business Ratchet Gate）**。

---

## 0. 官方用户与架构 Wiki（新版结构化知识库）

面向初学者与资深系统集成者的结构化导航手册（支持在线浏览与 Chromium 高清图文排版渲染）：

| 章节 | 文档入口 | 核心议题 |
| :--- | :--- | :--- |
| **Wiki 首页** | [`wiki/Home.md`](wiki/Home.md) | 全局架构图、核心分层原则、速查命令与知识库导览 |
| **第 1 篇** | [`wiki/01-Quickstart-and-Install.md`](wiki/01-Quickstart-and-Install.md) | 5 分钟上手、单根目录拓扑（`~/super_agent/<profile>`）、安装、升级、回滚与清理 |
| **第 2 篇** | [`wiki/02-Business-Overlay-Guide.md`](wiki/02-Business-Overlay-Guide.md) | 业务知识挂载、`SOUL.generic.md` 叠加层、MCP 挂载、Skills 扩展与 9 角色团队装配 |
| **第 3 篇** | [`wiki/03-Multi-Agent-and-StateGraph.md`](wiki/03-Multi-Agent-and-StateGraph.md) | 前后台协同边界、8 大官方研发角色（`DEV_ROLES`）、Raven 路径隔离与 StateGraph |
| **第 4 篇** | [`wiki/04-Feishu-Cards-and-Delivery.md`](wiki/04-Feishu-Cards-and-Delivery.md) | CardKit 2.0 状态机、单在途合并、实时进度流、Markdown 与 Chromium 图文交付 |
| **第 5 篇** | [`wiki/05-Operations-and-Troubleshooting.md`](wiki/05-Operations-and-Troubleshooting.md) | 布局感知运维（`_layout.sh`）、`doctor` 体检、脱敏召回诊断与故障处置 FAQ |

---

## 1. 核心技术规范（必读总览）

| 规范文档 | 内容概要 | 目标读者 |
| :--- | :--- | :--- |
| [`TECH_MANUAL.md`](TECH_MANUAL.md) | **Super Agent 技术手册**：大模型原理、执行边界、持久任务池、并发与恢复语义、长期记忆、安全威胁模型、SLO 与故障处置矩阵。 | 架构师、全栈开发、SRE、安全审计 |
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
| [`PACKAGE_MAP.md`](../agentcore/PACKAGE_MAP.md) | agentcore 逻辑领域架构与模块拓扑映射（7 大领域与 103 个模块归属） | `agentcore/` |
| [`ONE_COMMAND_INSTALL.md`](ONE_COMMAND_INSTALL.md) | 一键安装与整包升级：受管单根目录、原子切换、版本账本与回滚 | `install/superagent.sh`、`tools/superagent.py` |
| [`MIGRATION.md`](MIGRATION.md) | 八层迁移清单 + 资产表：跨环境整仓拉起标准 | `install/`、`docker/` |
| [`POOL_RUNTIME.md`](POOL_RUNTIME.md) | 持久任务池：状态迁移、依赖锁、租约、结果 outbox 与调度后端 | `agentcore/pool.py`、`worker.py` |
| [`COMPLETION_DELIVERY.md`](COMPLETION_DELIVERY.md) | 结果回投与完成态投递：origin 寻址、送达回读与三态语义 | `agentcore/completion_delivery.py`、`direct_completion.py` |
| [`COLLEAGUE_UNIFICATION_GUIDE.md`](COLLEAGUE_UNIFICATION_GUIDE.md) | 研发同事体系整合：8 大岗位契约、工作区锁与晚期绑定 | `agentcore/colleague_dev.py`、`dev_runner.py` |
| [`LONG_TASK_INTERACTION_GUIDE.md`](LONG_TASK_INTERACTION_GUIDE.md) | 长任务流式交互与卡片构建：CardKit 2.0、单在途更新与 drain 屏障 | `agentcore/cardkit2_renderer.py`、`card_stream.py` |
| [`ATTEMPT_STOP_CONTRACT.md`](ATTEMPT_STOP_CONTRACT.md) | 启动许可、Subreaper 进程监管器停止回执与在途任务隔离 | `agentcore/attempt_supervisor.py` |
| [`DISPATCH_ANALYSIS.md`](DISPATCH_ANALYSIS.md) | 派工协议、执行计划门禁与只读分析追溯事实 | `agentcore/analysis_runner.py` |
| [`REDEPLOY.md`](REDEPLOY.md) | 重新部署、静态复验与工具注册核对步骤 | `tools/redeploy_check.py` |
| [`RELEASE_READINESS.md`](RELEASE_READINESS.md) | 发版发布核对清单、已验证修复与严格门禁 | `tests/`、`docker/verify.sh` |
| [`RELEASE_CAPABILITIES.md`](RELEASE_CAPABILITIES.md) | 源码能力面与发版核对：按模块分组标注证据层级与验收出口 | `agentcore/`、`plugins/` |
| [`FRONTDESK_BOUNDARY.md`](FRONTDESK_BOUNDARY.md) | 前台权限边界、可信上下文绑定与未授权拦截 | `patches/`、`runtime/` |
| [`MEMORY_IDENTITY_ROUTES.md`](MEMORY_IDENTITY_ROUTES.md) | 长期记忆个人身份路由、独立 Worker 隔离与撤销机制 | `agentcore/memory/` |
| [`MODEL_CONTEXT.md`](MODEL_CONTEXT.md) | 运行时动态探测模型上下文窗口，杜绝硬编码 | `agentcore/model_context.py` |

---

## 4. 运行面工程目录职责边界契约（Directory Boundaries）

系统运行面严格遵循单一职责原则，划分为四大核心目录：

| 目录 | 职责定义 | 代表文件与作用 |
| :--- | :--- | :--- |
| [`install/`](../install/) | **引导器与外层 Shell 包装器 (Bootstrap & Shell Wrappers)** | `superagent.sh`（一键安装入口）、`install.sh`（底层幂等安装器）、`bootstrap_dependencies.py`（依赖预取器）、`templates/`（systemd 模板与 `SOUL.generic.md`）。专职新机器从零拉起底座。 |
| [`tools/`](../tools/) | **操作员 CLI 与开发者工具集 (Operator CLIs & Tools)** | `superagent.py`（统一生命周期工具：plan/install/update/rollback/doctor/pack）、`redeploy_check.py` 等。专职运维日常交互与发版管理。 |
| [`checks/`](../checks/) | **就地环境验收探针与轻量冒烟 (Verification Probes & Smoke Tests)** | `prefer_d7_smoke.py`、`feature_check.sh`、`deployment_layered_audit.py`、`model_context_check.sh`、`systemd_containment_probe.py`。专职就地环境验收与只读体检。 |
| [`scripts/ops/`](../scripts/ops/) | **宿主生命周期与集群运维脚本 (Host Lifecycle Shell Scripts)** | `_layout.sh`（多拓扑感知推断器）、`deploy_production.sh`（发布构建与分发）、`backup_and_rollback.py`（物理备份与回滚）、`uninstall-legacy.sh`（遗留服务清理）。专职集群 SRE 运维。 |

---

## 5. 专项设计规格与落地报告（Superpowers）

记录架构重大跃迁的设计初衷与实施闭环：

- **实施计划（待执行，不是能力验收声明）**：
  - [`TRUSTED_AUTONOMOUS_EXECUTION_PLAN.md`](TRUSTED_AUTONOMOUS_EXECUTION_PLAN.md)：可信自主执行 MVP，按 M0—M5 推进真实判定、执行边界、研发闭环、故障恢复、真实交付与受控经验复用。
- **设计规格（Specs）**：
  - [`superpowers/specs/2026-11-07-single-writer-deployment-design.md`](superpowers/specs/2026-11-07-single-writer-deployment-design.md)：单一写者部署路径与能力默认值收敛设计（过渡历史规格）。
  - [`superpowers/specs/2026-11-08-ops-scripts-layout-aware-design.md`](superpowers/specs/2026-11-08-ops-scripts-layout-aware-design.md)：官方运维脚本布局感知改造与真相源解析架构。
- **收尾报告（Reports）**：
  - [`superpowers/reports/2026-11-08-v2-convergence-and-docs-polish-report.md`](superpowers/reports/2026-11-08-v2-convergence-and-docs-polish-report.md)：v2.0 单根架构收敛、文档对齐与全仓验证交付报告。

---

## 6. 专题参考与历史演进文档（Reference & Historical Deep Dive）

| 文档 | 定位与内容说明 |
| :--- | :--- |
| [`SUPERAGENT_ARCHITECTURE_DEEP_DIVE.md`](SUPERAGENT_ARCHITECTURE_DEEP_DIVE.md) | 架构深度解析（历史基线 `d6e4271` 深潜参考，最新权威以 [`ARCHITECTURE.md`](ARCHITECTURE.md) 为准） |
| [`LANDSCAPE.md`](LANDSCAPE.md) | 智能体生态全景对比：Cordis、DSH 与通用智能体基础设施定位分析 |
| [`UPSTREAM_CATALOG.md`](UPSTREAM_CATALOG.md) | 上游运行时能力目录与依赖映射表 |
| [`NIGHTLY_SELF_REVIEW.md`](NIGHTLY_SELF_REVIEW.md) | 夜间巡检自愈审查规范与定时作业守卫 |
| [`LONG_TASK_INTERACTION_HANDOVER.md`](handoff/LONG_TASK_INTERACTION_HANDOVER.md) | 长任务流式交互早期交接备忘录 |
| [`FRONTDESK_DISPATCH_PLAN.md`](handoff/FRONTDESK_DISPATCH_PLAN.md) | IM 前台、后台调度、同事与记忆收敛计划（阶段收敛规划） |
| [`DEPLOYMENT_CLOSEOUT_PLAN.md`](handoff/DEPLOYMENT_CLOSEOUT_PLAN.md) | 升级收尾与部署计划（历史阶段指导） |
| [`BUSINESS_ADAPTER_REPAIR_PLAN.md`](handoff/BUSINESS_ADAPTER_REPAIR_PLAN.md) | 业务适配与运行状态一致性修复计划（历史阶段指导） |

---

## 7. 历史现场与验证报告（Historical Reports）

位于 `docs/reports/` 目录下，记录历史排查、断言数据与攻坚过程。
> **注意**：历史报告包含当时特定的现场测试数据与日志指纹，**仅用于追溯决策依据**；当前系统架构与运行时行为一律以最新代码和上述活文档为准。
