# agentcore 逻辑领域架构与模块拓扑映射（Package Map）

本文档定义 `agentcore/` 核心引擎的七大逻辑领域划分、职责边界与模块映射表。为确保全量历史测试集、插件系统、安装器与外部调用方的 100% 向后兼容性，所有模块在 Python 导入层面统一保持顶层扁平入口（`from agentcore.<module> import ...`），在架构逻辑层面按业务无关的高内聚低耦合原则划分为七大领域。

---

## 1. 核心设计原则

1. **绝对业务中立性（Zero Business Literals）**：
   本核心包严禁出现任何特定企业的领域名称、型号代际、特定 IM 厂商字面量或环境主机指纹（由 `tests/test_business_neutrality.py` 与 `tests/test_repo_neutrality.py` 双重门禁强制保障）。所有环境配置与业务特化必须通过 `MemoryConfig`、`SOUL.generic.md` 模板与外部配置注入。
2. **单一真相源与因果确定性（Deterministic Causality）**：
   任务生命周期严格由持久任务池（`pool.py`）仲裁，跨进程并发通过 `fcntl.flock` 锁定工作区，状态跳转由确定性状态图引擎（`state_graph.py`）驱动。
3. **扁平物理布局 + 领域逻辑组织**：
   保持 `agentcore/*.py` 扁平文件路径，杜绝路径重构导致的跨仓导入破坏；通过本规范对模块进行严密的领域归类与职责隔离。

---

## 2. 七大逻辑领域分类

```
agentcore/
├── [1. Colleagues]  多智能体协同与研发流水线 (colleague_*, dev_runner)
├── [2. Cards]       卡片交互与流式渲染 (card_*, cardkit2_renderer, task_card*)
├── [3. Delivery]    终态回投、送达回执与证据链 (completion_*, delivery_*, direct_completion)
├── [4. Execution]   任务调度、进程监管与执行门禁 (pool, worker, attempt_supervisor, analysis_runner)
├── [5. Memory]      长期记忆体系与可塑性治理 (memory/, memory_*, colleague_memory_cli)
├── [6. Liveness]    状态图、存活心跳与 SRE 观测 (state_graph, liveness*, structured_logging, ops_alerts*)
└── [7. Governance]  模型路由、配置覆盖与安全包络 (model_*, profile_*, baseline_config, safety_envelope)
```

---

## 3. 详细模块职责映射表

### 领域 1：同事协同与多智能体组织学 (Colleagues & Multi-Agent Orchestration)
专职负责常驻智能体团队的岗位契约、工作区物理隔离（Worktree）、晚期绑定与跨智能体流水线协作。

| 模块文件 | 领域归属 | 核心职责 |
| :--- | :--- | :--- |
| `colleague_dev.py` | Colleagues | 8 大官方研发岗位契约（架构师、用例大师、红队挑刺等）与路径访问权限控制（`RolePathOwnership`） |
| `dev_runner.py` | Colleagues | 研发同事工作树运行器，基于 `fcntl.flock` 实现单一写者与沙箱执行 |
| `colleague_registry.py` | Colleagues | 同事注册表、元数据发现与 Profile 级联加载 |
| `colleague_provisioner.py` | Colleagues | 同事环境初始化、工作区预备与资产落位 |
| `colleague_pool.py` | Colleagues | 同事专属任务池封装与任务隔离 |
| `colleague_worker.py` | Colleagues | 同事工作进程监听与任务消费 |
| `colleague_dispatch.py` | Colleagues | 同事间任务派发协议与上下文透传 |
| `colleague_conversation.py` | Colleagues | 同事会话生命周期管理与上下文维护 |
| `colleague_dialogue.py` | Colleagues | 同事间结构化对话交互接口 |
| `colleague_dialogue_history.py` | Colleagues | 对话历史持久化与检索 |
| `colleague_gateway_bootstrap.py` | Colleagues | 同事网关启动引导与运行时上下文配置 |
| `colleague_gateway_dispatch.py` | Colleagues | 同事网关消息路由与派单分发 |
| `colleague_gateway_host.py` | Colleagues | 同事宿主进程适配与通信守护 |
| `colleague_gateway_service.py` | Colleagues | 同事网关 systemd 用户服务包装 |
| `colleague_hermes_runner.py` | Colleagues | 外部智能体引擎运行时适配器 |
| `colleague_hermes_staging.py` | Colleagues | 外部智能体运行暂存区隔离与凭据挂载 |
| `colleague_pack.py` | Colleagues | 同事资产打包、分发与签名核验 |
| `colleague_queue_delivery.py` | Colleagues | 同事队列消费与完成态回执路由 |
| `colleague_readonly_delivery.py` | Colleagues | 只读分析同事的结果投递链路 |
| `colleague_readonly_dialogue.py` | Colleagues | 只读分析同事的受限交互协议 |
| `colleague_readonly_queue.py` | Colleagues | 只读分析同事的受限任务队列 |
| `colleague_readonly_runtime.py` | Colleagues | 只读分析同事的无副作用运行时 |
| `colleague_safe_gateway_backend.py` | Colleagues | 安全网关后端契约实现 |
| `colleague_tool_gateway_backend.py` | Colleagues | 工具网关后端契约实现 |
| `colleague_shared_memory.py` | Colleagues | 跨同事共享只读记忆绑定与访问控制 |
| `colleague_workflow_delivery.py` | Colleagues | 同事工作流阶段性产物交付 |
| `colleague_workflow_runtime.py` | Colleagues | 同事工作流执行期状态机驱动 |
| `colleague_workflow_store.py` | Colleagues | 工作流拓扑定义与状态快照存储 |
| `colleague_cancel_guard.py` | Colleagues | 级联取消传播与孤儿智能体回收守卫 |
| `colleague_catalog.py` | Colleagues | 平台可用同事能力名录检索 |
| `colleague_containment.py` | Colleagues | 物理隔离围栏与未授权越权操作探测 |
| `colleague_evidence.py` | Colleagues | 研发全过程证据链收集与结构化归档 |

---

### 领域 2：卡片交互与流式前端 (Cards & Stream Interaction)
专职负责 IM 渠道端富交互卡片、单在途流式渲染与状态回执更新。

| 模块文件 | 领域归属 | 核心职责 |
| :--- | :--- | :--- |
| `cardkit2_renderer.py` | Cards | 原生 CardKit 2.0 状态机渲染引擎，保障 24 项卡片要素合规与流式屏障 |
| `card_stream.py` | Cards | 单在途流式进度更新通道与 drain 队列排空控制 |
| `card_emit.py` | Cards | 卡片实体发送与平台适配桥接 |
| `card_markdown.py` | Cards | 卡片内嵌 Markdown 转换与安全过滤 |
| `card_store.py` | Cards | 历史卡片快照、版本索引与去重缓存 |
| `task_card.py` | Cards | 任务状态卡片契约数据结构与序列化 |
| `task_card_bridge.py` | Cards | 过程心跳卡片与终态卡片补丁解耦桥接器（`TaskCardBridge`） |
| `stalled_card_bridge.py` | Cards | 停滞卡片超时检测、降级处理与死信标记 |
| `feishu_transport.py` | Cards | 平台传输层适配（适配层抽象，保持业务无关） |

---

### 领域 3：交付与事实追溯 (Delivery, Receipts & Traceability)
专职负责完成态事实回投、Origin 寻址、送达回执与证据链清洗。

| 模块文件 | 领域归属 | 核心职责 |
| :--- | :--- | :--- |
| `completion_delivery.py` | Delivery | 任务完成回执投递调度器，处理 origin 寻址与重试退避 |
| `direct_completion.py` | Delivery | 终态直投通道（硬链接保证与非侵入式送达） |
| `completion_backfill.py` | Delivery | 存量积压完成回执补投与状态同步 |
| `delivery_sanitizer.py` | Delivery | 交付物安全净化（内网 IP、敏感路径与凭据脱敏） |
| `deliverable_mode.py` | Delivery | 交付模式判定与通道策略选择 |
| `markdown_delivery.py` | Delivery | 纯文本与 Markdown 结果消息投递 |
| `artifact_retention.py` | Delivery | 交付工件留存生命周期与滚动清理策略 |
| `kanban_contract.py` | Delivery | 看板任务追踪契约与状态同步协议 |
| `colleague_delivery_cli.py` | Delivery | 终态任务交付与结果回投 CLI 运维工具 |

---

### 领域 4：任务执行与进程监督 (Execution Engine & Supervision)
专职负责任务池调度、子进程生命周期监管、执行门禁与抢占控制。

| 模块文件 | 领域归属 | 核心职责 |
| :--- | :--- | :--- |
| `pool.py` | Execution | 持久任务池（SQLite WAL），租约管理、依赖锁、重试与结果 outbox |
| `worker.py` | Execution | 任务执行消费进程，支持标准库与 FlowCoro 调度器 |
| `scheduler.py` | Execution | 有界并发调度控制器（`AgentScheduler`） |
| `attempt_supervisor.py` | Execution | 启动许可管理、Subreaper 子进程树监管与强制停止证明 |
| `analysis_runner.py` | Execution | 只读分析任务执行器，产出结构化引用证据 |
| `exec_runner.py` | Execution | 通用可执行任务外壳与环境隔离 |
| `session_execution.py` | Execution | 会话执行上下文隔离与资源配额管理 |
| `verification_gate.py` | Execution | 证据驱动测试验证门禁，防范伪绿与未测试交付 |
| `plan_gate.py` | Execution | 执行计划合法性前置审查门禁 |
| `barge_in_control.py` | Execution | 用户实时插话打断控制（Barge-in）与在途任务挂起/抢占 |
| `safety_envelope.py` | Execution | 运行时安全信封与高危操作熔断机制 |

---

### 领域 5：长期记忆体系 (Long-Term Memory Subsystem)
专职负责双路检索（向量 + BM25/FTS5）、突触可塑性强化、事实门禁与代际硬隔离。

| 模块文件 / 目录 | 领域归属 | 核心职责 |
| :--- | :--- | :--- |
| `memory/` | Memory | 核心记忆子包（含 `memory/memory_store.py`, `memory/hybrid_recall.py`, `memory/recall_service.py`, `memory/memory_gate.py`, `memory/memory_surprise.py`, `memory/memory_plasticity.py`, `memory/memory_consolidate.py`, `memory/memory_graph.py`, `memory/synapse.py`, `memory/vec_index.py` 等；`localvec` 位于 `plugins/localvec/`） |
| `colleague_memory_cli.py`| Memory | 记忆库操作 CLI 与运维探针 |
| `memory_evaluation.py` | Memory | 记忆召回准确率与相关性基准评估 |
| `memory_footprint.py` | Memory | 记忆库存储体积与索引膨胀监控 |
| `memory_hygiene.py` | Memory | 记忆库定期去重、垃圾清理与健康对账 |

---

### 领域 6：状态图引擎与系统韧性 (State Graph, Liveness & SRE Observability)
专职负责确定性状态流转、服务存活巡检、停滞检测与结构化观测。

| 模块文件 | 领域归属 | 核心职责 |
| :--- | :--- | :--- |
| `state_graph.py` | Liveness | 纯 Python 零依赖确定性状态图引擎（Typed Reducers, Conditional Edges, Checkpointers, Stagnation Guard） |
| `liveness.py` | Liveness | 核心服务存活判定、心跳记录与假死侦测 |
| `liveness_daemon.py` | Liveness | 定时存活扫描守护进程（systemd 集成） |
| `tcp_watchdog.py` | Liveness | 网络端口与协议通道连通性看门狗 |
| `trajectory_ledger.py` | Liveness | 任务状态跳变轨迹账本与脱窗审计 |
| `structured_logging.py` | Liveness | 集中式结构化日志引擎（ContextFilter, 轮转归档） |
| `ops_alerts.py` | Liveness | 运维告警分发中心 |
| `ops_alert_daemon.py` | Liveness | 告警聚合与去抖守护进程 |
| `ops_alert_senders.py` | Liveness | 告警外部通道投递适配器 |
| `proactive_watch.py` | Liveness | 主动探针与外部资源哨兵监控 |
| `proactive_hooks.py` | Liveness | 主动事件触发挂钩与回调链 |
| `process_pressure.py` | Liveness | 宿主 CPU / 内存系统负载压力感知 |
| `capability_guard.py` | Liveness | 能力基线底线巡检与漂移告警守卫 |
| `capability_guard_cli.py` | Liveness | 能力基线巡检与漂移告警运维 CLI |

---

### 领域 7：配置、治理与模型拓扑 (Governance, Profiles & Routing)
专职负责模型路由、上下文探测、Profile 覆盖与平台基础设施。

| 模块文件 | 领域归属 | 核心职责 |
| :--- | :--- | :--- |
| `__init__.py` | Governance | agentcore 核心包入口，统一导出业务中立性与核心引擎接口 |
| `baseline_config.py` | Governance | 通用基础配置加载器与环境变量合并 |
| `profile_spec.py` | Governance | 受管 Profile 规范、单根目录推断与布局自适应 |
| `profile_overlay.py` | Governance | 业务叠加层注入与安全只读合并机制 |
| `model_routing.py` | Governance | 模型多级动态路由引擎（大/小模型按能力分流） |
| `model_context.py` | Governance | 运行时动态探测模型上下文窗口，杜绝硬编码 pin |
| `model_latency_report.py` | Governance | 模型端点延迟与可用性体检报告 |
| `subagent_protocol.py` | Governance | 子智能体提示词装配与防级联失败协议 |
| `failure_mender.py` | Governance | 失败因果归因分类器与自愈决策链 |
| `skill_forge.py` | Governance | 动态技能锻造与可复用工具封装 |
| `preflight.py` | Governance | 启动前环境自检与依赖核验 |
| `middleware_policy.py` | Governance | 中间件策略过滤器链 |
| `lan_cluster.py` | Governance | 局域网集群节点发现与心跳对账 |
| `pipeline_dag.py` | Governance | 任务流水线有向无环图调度器 |
| `document_intent.py` | Governance | 文档生成意图解析与格式引导 |
| `flash_diagram_draft.py` | Governance | 架构图快速草绘生成器 |
| `follow_up_store.py` | Governance | 待跟进事项持久化存储 |
| `hitl_bridge.py` | Governance | 人在回路（HITL）交互确认桥梁 |
| `task_progress.py` | Governance | 任务进度细粒度跟踪与计算 |
| `acceptance_capture.py` | Governance | 验收动作捕捉器 |
| `acceptance_evidence.py` | Governance | 验收证据结构化封装 |
| `ccp_config.py` | Governance | 控制平面配置契约定义 |
| `ccp_analysis_model.py` | Governance | 控制平面分析模型封装 |
| `progressive_delivery.py` | Governance | 渐进式交付流水线控制器 |
| `codegraph/` | Governance | 代码关系图谱分析引擎子包 |
| `learning/` | Governance | 历史会话经验提炼与反思学习子包 |

---

## 4. 导入契约与演进规范

1. **导入绝对不变**：所有外部调用代码统一通过 `from agentcore.<module> import ...` 或 `import agentcore.<module>` 引用。
2. **新增模块准入**：凡新增 `agentcore/` 下的 `.py` 文件，必须在提交前归入上述 7 大领域之一，并在本文件中更新职责定义。
3. **架构演进原则**：如未来需要物理拆分为子包，须通过 `agentcore/<module>.py` 薄代理层实现 100% 符号 re-export，绝不允许破坏现有调用方的直接导入。
