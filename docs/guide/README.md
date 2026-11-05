# 先进 Agent 架构与工程实践指南

本目录面向希望从 `super-agent` 深入学习**生产级常驻智能体（Always-on Production Agent）**核心架构、设计模式与工程落地方法的开发者与架构师。

与仓库中面向运维操作与发版核对的契约文档不同，本系列指南聚焦于回答三个核心问题：
1. **Why（问题域与演进背景）**：为什么常见的开源玩具 Agent（简单的 ReAct 循环、内存队列、无约束多智能体群聊、盲目写入的向量数据库）在真实生产环境中会迅速崩溃？
2. **How（核心机制与工程解耦）**：`super-agent` 如何通过确定性的系统工程手段（持久池、围栏、双阶段主动探针、模型路由不变量、代际记忆隔离、物理证据门禁）驯服概率性的大语言模型？
3. **Where（源码映射与动手验证）**：每一项架构设计对应哪个代码模块、哪条架构决策记录（ADR）以及哪组自动化测试？

---

## 阅读路径

建议按以下顺序递进阅读：

| 篇目 | 文档 | 核心主题 | 关键源码模块 |
| :--- | :--- | :--- | :--- |
| **第 0 篇** | [`00_ARCHITECTURE_OVERVIEW.md`](00_ARCHITECTURE_OVERVIEW.md) | **全景导览：从玩具 Agent 到生产级常驻智能体**<br>剖析玩具 Agent 的六大生产死穴、系统十二层架构全景、端到端请求生命周期，以及“通用本体 + 声明式业务叠加层（`BusinessProfileSpec`）”的解耦哲学。 | `agentcore/profile_spec.py`<br>`install/setup_profile.py`<br>`identity/SOUL.generic.md` |
| **第 1 篇** | [`01_COLLEAGUE_AND_ORCHESTRATION.md`](01_COLLEAGUE_AND_ORCHESTRATION.md) | **多智能体组织学：同事制、模型路由与工作区隔离**<br>对比同事制、编排制与对等制；解析主同事控场契约、8 大专业研发同事岗位、双引擎执行器（Hermes + Pi fallback）、Git Worktree + `fcntl.flock` 隔离，以及三级模型路由与六大架构不变量（`ADR-S1` ~ `ADR-S6`）。 | `agentcore/colleague_dev.py`<br>`agentcore/dev_runner.py`<br>`agentcore/model_routing.py`<br>`install/worker/dev_exec_runner.py` |
| **第 2 篇** | [`02_DURABLE_RUNTIME_AND_LIVENESS.md`](02_DURABLE_RUNTIME_AND_LIVENESS.md) | **长任务运行时：持久队列、插话抢占与主动探针**<br>拆解前后台分离架构、SQLite 持久任务池与租约、Attempt Fencing 防旧任务围栏、Linux Subreaper 进程树收割、活体心跳账本、中途插话控制（Barge-in），以及零 LLM 消耗的双阶段主动巡检探针（Proactive Hooks）。 | `agentcore/pool.py`<br>`agentcore/worker.py`<br>`agentcore/liveness.py`<br>`agentcore/barge_in_control.py`<br>`agentcore/proactive_hooks.py` |
| **第 3 篇** | [`03_MEMORY_ENGINEERING.md`](03_MEMORY_ENGINEERING.md) | **生产级长期记忆：双路召回、代际硬隔离与容量治理**<br>揭示为什么不能让 LLM 抽取器黑盒覆盖事实；详解 `workspace_id` 代际分桶硬隔离、独立 `fact_tags` 表结构、向量与词法双路召回、置信度衰减与突触账本，以及常驻只读视图的内存预算监控。 | `agentcore/memory/memory_store.py`<br>`agentcore/memory/vec_index.py`<br>`agentcore/memory_footprint.py`<br>`tools/recall_combined.py` |
| **第 4 篇** | [`04_EVIDENCE_AND_VERIFICATION.md`](04_EVIDENCE_AND_VERIFICATION.md) | **防幻觉与确定性工程：证据门禁、流式屏障与干净室验证**<br>阐述如何用确定性代码守住交付底线：分析与研发交付的物理证据链校验、CardKit 2.0 单在途合并与 `drain()` 屏障、服务端回读核对、零业务词棘轮门禁，以及死端口请求转储的 Prompt 注入取证。 | `agentcore/analysis_runner.py`<br>`agentcore/colleague_evidence.py`<br>`agentcore/cardkit2_renderer.py`<br>`tests/test_repo_neutrality.py`<br>`checks/identity_injection_check.sh` |

---

## 动手验证与伴读建议

阅读本系列文档时，推荐结合仓库自带的自动化验证工具边读边跑，直观感受每一层机制的物理证据：

```bash
# 1. 运行全仓静态检查、业务无关性棘轮门禁与核心单元测试（约 30 秒）
bash verify.sh

# 2. 单独验证模型路由与六大架构不变量（ADR-S1 ~ ADR-S6）
python3 -m pytest tests/test_model_routing.py -q

# 3. 单独验证声明式业务专家包（BusinessProfileSpec）与 systemd 渲染
python3 -m pytest tests/test_profile_spec.py -q

# 4. 单独验证零 LLM 快速路径的双阶段主动探针引擎
python3 -m pytest tests/test_proactive_hooks.py -q

# 5. 使用内置图文渲染工具将本目录任一指南编译为自包含 HTML 与长图
python3 tools/arch_doc.py docs/guide/00_ARCHITECTURE_OVERVIEW.md -o /tmp/guide-preview
```

