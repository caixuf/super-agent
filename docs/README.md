# 文档索引

先读这五份，其余按需。

## 活文档（描述当前架构，跟着代码一起改）

| 文档 | 讲什么 |
| :--- | :--- |
| [`MIGRATION.md`](MIGRATION.md) | 八层迁移清单 + 资产表：一个仓要立起来缺哪些东西 |
| [`PLUGINS.md`](PLUGINS.md) | 插件 / MCP / 工具集的注册方式 |
| [`POOL_RUNTIME.md`](POOL_RUNTIME.md) | 持久任务池：依赖、租约、结果 outbox、调度后端 |
| [`ATTEMPT_STOP_CONTRACT.md`](ATTEMPT_STOP_CONTRACT.md) | 启动许可、监管器停止回执与旧在途任务的未知状态；不是任意代码沙箱 |
| [`DISPATCH_ANALYSIS.md`](DISPATCH_ANALYSIS.md) | 派工与只读分析链路 |
| [`REDEPLOY.md`](REDEPLOY.md) | 重新部署与复验步骤 |
| [`RELEASE_READINESS.md`](RELEASE_READINESS.md) | 本次发版核对：已验证修复、真实接线与未完成门禁 |
| [`RELEASE_CAPABILITIES.md`](RELEASE_CAPABILITIES.md) | 完整版发版能力表：通用运行、业务适配、实例部署及逐项验收口径；不是已通过声明 |
| [`FRONTDESK_BOUNDARY.md`](FRONTDESK_BOUNDARY.md) | 候选宿主的前台权限边界、可信配置、执行接缝与限制；未生产启用 |
| [`MEMORY_IDENTITY_ROUTES.md`](MEMORY_IDENTITY_ROUTES.md) | IM 个人记忆身份路由、独立 Worker、撤销与部署边界；不替代整体多租户审计 |

配套的机器可读约定在仓库根部：`verify.sh`（单一自检入口）、`install/install.sh`（幂等安装器）、`docker/`（干净室验证）。

## 实施规划（不是当前能力说明）

- [`FRONTDESK_DISPATCH_PLAN.md`](FRONTDESK_DISPATCH_PLAN.md)：2026-11-02 的收敛计划。
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
| `agentcore/memory/memory_gate.py` | 提炼结果入库门禁（三道校验） | 未接线 |
| `agentcore/memory/memory_plasticity.py` | 突触可塑性反馈（按任务终态强化/抑制） | 未接线 |
| `agentcore/memory/memory_sync.py` | 记忆库增量同步（导出 A 有 B 无的事实） | 未接线 |
| `agentcore/feishu_transport.py` 的 `FeishuCardKitAdapter` | 用卡片实体做流式进度更新 | 未接线（且三处请求体与官方契约不符，接线前须重写） |
| `agentcore/cardkit2_renderer.py` 的 `CardStreamRenderer` | 原生 CardKit 2.0 渲染与单在途更新 | 未接线（无宿主插件/transport 接线；**HFC 为唯一生产者**） |

前三个记忆模块都在 docstring 顶部标了 `STATUS: 未接线`。
**不要按「记忆引擎已具备这些能力」来理解它**——接线前需要先补调用方和对应测试。
原生卡片组件的存在也不代表已替代 HFC；不得在未完成唯一发送者迁移与真实
渠道验收前另开一个生产者。
