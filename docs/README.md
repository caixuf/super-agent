# 文档索引

> 完整技术体系全景与文档分类，请查阅权威主索引：[`INDEX.md`](INDEX.md)。新用户建议直接访问：[**官方用户与架构 Wiki**](wiki/Home.md)。

## 官方用户与架构 Wiki

- [`wiki/Home.md`](wiki/Home.md)：**Wiki 首页与全局导览** —— 架构全景、核心概念与章节索引
- [`wiki/01-Quickstart-and-Install.md`](wiki/01-Quickstart-and-Install.md)：**第 1 篇：5 分钟上手与单根部署**
- [`wiki/02-Business-Overlay-Guide.md`](wiki/02-Business-Overlay-Guide.md)：**第 2 篇：业务知识挂载指南**
- [`wiki/03-Multi-Agent-and-StateGraph.md`](wiki/03-Multi-Agent-and-StateGraph.md) ：**第 3 篇：前后台协同与 StateGraph**
- [`wiki/04-Feishu-Cards-and-Delivery.md`](wiki/04-Feishu-Cards-and-Delivery.md)：**第 4 篇：企业协作卡片与交付机制**
- [`wiki/05-Operations-and-Troubleshooting.md`](wiki/05-Operations-and-Troubleshooting.md)：**第 5 篇：运维排障与 FAQ**

## 核心技术规范

- [`TECH_MANUAL.md`](TECH_MANUAL.md)：**Super Agent 技术手册** —— 系统架构、大模型原理、任务与记忆、权限、安全、部署运维及验收规范。
- [`ARCHITECTURE.md`](ARCHITECTURE.md)：**v2.0 架构全景深潜** —— 单根架构拓扑（`~/super_agent/<profile>`）、十二层分层机制、同事制协作与执行保障。
- [`DEPLOY.md`](DEPLOY.md)：**生产部署与生命周期指南** —— 单根受管安装、身份暂存与业务叠加层挂载（`SOUL.generic.md`）、`hermes-current` 自动推断与干净室验证。
- [`PLUGINS.md`](PLUGINS.md)：**插件与工具面全景指南** —— 16 款插件（13 通用内建 + 3 受管外部）与 `localvec` 记忆提供方配置规范。
- [`OPERATIONS.md`](OPERATIONS.md)：**布局感知运维与对账指南** —— 布局自适应解析（`_layout.sh`）、遗留服务对账纠偏（`uninstall-legacy.sh`）与环境自愈重建。

## 先进 Agent 架构与工程实践指南（推荐学习入口）

面向希望深入理解本项目核心设计思想、架构取舍与生产级 Agent 工程技术的开发者：

- [`guide/README.md`](guide/README.md)：**指南总目录与伴读验证命令**
- [`guide/00_ARCHITECTURE_OVERVIEW.md`](guide/00_ARCHITECTURE_OVERVIEW.md)：**第 0 篇：全景导览 —— 从玩具 Agent 到生产级常驻智能体**
- [`guide/01_COLLEAGUE_AND_ORCHESTRATION.md`](guide/01_COLLEAGUE_AND_ORCHESTRATION.md)：**第 1 篇：多智能体组织学 —— 同事制、模型路由与工作区隔离**
- [`guide/02_DURABLE_RUNTIME_AND_LIVENESS.md`](guide/02_DURABLE_RUNTIME_AND_LIVENESS.md)：**第 2 篇：长任务运行时 —— 持久队列、插话抢占与主动探针**
- [`guide/03_MEMORY_ENGINEERING.md`](guide/03_MEMORY_ENGINEERING.md)：**第 3 篇：生产级长期记忆 —— 双路召回、代际硬隔离与容量治理**
- [`guide/04_EVIDENCE_AND_VERIFICATION.md`](guide/04_EVIDENCE_AND_VERIFICATION.md)：**第 4 篇：防幻觉与确定性工程 —— 证据门禁、流式屏障与干净室验证**

## 活文档（描述当前架构，跟着代码一起改）

| 文档 | 讲什么 |
| :--- | :--- |
| [`ONE_COMMAND_INSTALL.md`](ONE_COMMAND_INSTALL.md) | 一键安装与升级指南：受管单根、原子切换与回滚 |
| [`MIGRATION.md`](MIGRATION.md) | 八层迁移清单 + 资产表：一个仓要立起来缺哪些东西 |
| [`PLUGINS.md`](PLUGINS.md) | 插件 / MCP / 工具集的注册方式 |
| [`POOL_RUNTIME.md`](POOL_RUNTIME.md) | 持久任务池：依赖、租约、结果 outbox、调度后端 |
| [`ATTEMPT_STOP_CONTRACT.md`](ATTEMPT_STOP_CONTRACT.md) | 启动许可、监管器停止回执与旧在途任务的未知状态；不是任意代码沙箱 |
| [`DISPATCH_ANALYSIS.md`](DISPATCH_ANALYSIS.md) | 派工与只读分析链路 |
| [`REDEPLOY.md`](REDEPLOY.md) | 重新部署与复验步骤 |
| [`RELEASE_READINESS.md`](RELEASE_READINESS.md) | 本次发版核对：已验证修复、真实接线与未完成门禁 |
| [`RELEASE_CAPABILITIES.md`](RELEASE_CAPABILITIES.md) | 源码能力面与发版核对：按当前代码模块分组，标注证据层级与验收出口；不是已通过声明 |
| [`FRONTDESK_BOUNDARY.md`](FRONTDESK_BOUNDARY.md) | 候选宿主的前台权限边界、可信配置、执行接缝与限制；未生产启用 |
| [`MEMORY_IDENTITY_ROUTES.md`](MEMORY_IDENTITY_ROUTES.md) | IM 个人记忆身份路由、独立 Worker、撤销与部署边界；不替代整体多租户审计 |

配套的机器可读约定在仓库根部：`verify.sh`（单一自检入口）、`install/install.sh`（幂等安装器）、`docker/`（干净室验证）。

## 实施规划（不是当前能力说明）

- [`TRUSTED_AUTONOMOUS_EXECUTION_PLAN.md`](TRUSTED_AUTONOMOUS_EXECUTION_PLAN.md)：可信自主执行实施计划（2026-11-09，待执行）。
  定义 M0—M5 工作项、退出条件、首批最小交付与证据规则；不授权生产变更，不代表能力已验收。
- [`FRONTDESK_DISPATCH_PLAN.md`](handoff/FRONTDESK_DISPATCH_PLAN.md)：2026-11-02 的收敛计划。
  IM 前台只交互、派单与汇报，后台执行；按调度闭环、同事配置、记忆治理与
  真实渠道灰度分阶段验收。权限子项已开始隔离实现，整体尚未完成，
  不代表生产已完成迁移。

## 参考

| 文档 | 讲什么 |
| :--- | :--- |
| `MODEL_CONTEXT.md` | 模型上下文窗口怎么探测，不靠手写 pin |
| `CONFIG_MAP.md` | 配置段归属与并发语义（`runtime/` 下） |
| `BASELINE.md` | 通用能力基线（`runtime/` 下） |

## `docs/reports/` 是历史记录，不是当前架构

本次现场只读核对：[`2026-11-03_runtime-topology-audit.md`](reports/2026-11-03_runtime-topology-audit.md)。
它记录当时的有效 home、队列、服务和单主体记忆差异，不是持续健康或部署证明。

那个目录里是开发过程中的逐次验证记录（每次「做了什么 / 验了什么 / 结论」）。留下来的理由是它们含着当时的现场证据：某次崩溃的原始日志、某个判断的推翻过程、某个数字的测量方法。

**但它们的描述会过时。** 判断「现在是怎么工作的」，一律以 `agentcore/` 代码 + 上面的活文档为准；`reports/` 只在你想追溯「为什么当初这么定」时才看。

## 未接线的模块

以下模块已有设计但**没有接入主链路**，独立测试不代表实际启用：

| 模块 | 作用 | 状态 |
| :--- | :--- | :--- |
| `agentcore/memory/memory_gate.py` | 提炼结果入库门禁（三道校验） | 已接线（`write_fact(enforce_gate=True)`） |
| `agentcore/memory/memory_plasticity.py` | 突触可塑性反馈（按任务终态强化/抑制） | 已接线（`recall_facts(task_id=...)` -> `memory_trace` -> `Pool._terminal` / `apply_task_outcome`） |
| `agentcore/memory/memory_sync.py` | 记忆库增量同步（导出 A 有 B 无的事实并一站式同步） | 已接线（`sync_databases()` + `memory_sync.py sync` CLI） |
| `agentcore/feishu_transport.py` 的 `FeishuCardKitAdapter` | 用卡片实体做流式进度更新 | 未接线（且三处请求体与官方契约不符，接线前须重写） |
| `agentcore/cardkit2_renderer.py` 的 `CardStreamRenderer` | 原生 CardKit 2.0 渲染与单在途更新 | 作为库提供；宿主接线见 `patches/feishu_card_lifecycle.patch` |

卡片与完成态投递由 `agentcore/` 原生链路承担，仓库不再维护外部卡片 sidecar 集成；
一个交互只能有一个生产者，替换发送者须在同一次变更里完成迁移与真实渠道验收。
