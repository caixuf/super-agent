# 文档索引

先读这五份，其余按需。

## 活文档（描述当前架构，跟着代码一起改）

| 文档 | 讲什么 |
| :--- | :--- |
| [`MIGRATION.md`](MIGRATION.md) | 八层迁移清单 + 资产表：一个仓要立起来缺哪些东西 |
| [`PLUGINS.md`](PLUGINS.md) | 插件 / MCP / 工具集的注册方式 |
| [`POOL_RUNTIME.md`](POOL_RUNTIME.md) | 持久任务池：依赖、租约、结果 outbox、调度后端 |
| [`DISPATCH_ANALYSIS.md`](DISPATCH_ANALYSIS.md) | 派工与只读分析链路 |
| [`REDEPLOY.md`](REDEPLOY.md) | 重新部署与复验步骤 |

配套的机器可读约定在仓库根部：`verify.sh`（单一自检入口）、`install/install.sh`（幂等安装器）、`docker/`（干净室验证）。

## 参考

| 文档 | 讲什么 |
| :--- | :--- |
| `MODEL_CONTEXT.md` | 模型上下文窗口怎么探测，不靠手写 pin |
| `CONFIG_MAP.md` | 配置段归属与并发语义（`runtime/` 下） |
| `BASELINE.md` | 通用能力基线（`runtime/` 下） |

## `docs/reports/` 是历史记录，不是当前架构

那个目录里是开发过程中的逐次验证记录（每次「做了什么 / 验了什么 / 结论」）。留下来的理由是它们含着当时的现场证据：某次崩溃的原始日志、某个判断的推翻过程、某个数字的测量方法。

**但它们的描述会过时。** 判断「现在是怎么工作的」，一律以 `agentcore/` 代码 + 上面的活文档为准；`reports/` 只在你想追溯「为什么当初这么定」时才看。

## 未接线的模块

仓里有两个模块已设计但**没有接入主链路**，代码与测试均零引用：

| 模块 | 作用 | 状态 |
| :--- | :--- | :--- |
| `agentcore/memory/memory_gate.py` | 提炼结果入库门禁（三道校验） | 未接线 |
| `agentcore/memory/memory_plasticity.py` | 突触可塑性反馈（按任务终态强化/抑制） | 未接线 |
| `agentcore/memory/memory_sync.py` | 记忆库增量同步（导出 A 有 B 无的事实） | 未接线 |

三者都在模块 docstring 顶部标了 `STATUS: 未接线`。**不要按「记忆引擎已具备这些能力」来理解它**——接线前需要先补调用方和对应测试。