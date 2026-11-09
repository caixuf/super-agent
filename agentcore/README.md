# agentcore —— 通用多智能体与任务引擎核心

`agentcore` 是 Super Agent 平台的业务中立核心底座，提供常驻长程智能体（Always-on Production Agent）所需的任务池持久化、研发同事制协作、富交互流式渲染、终态交付回投、长期记忆治理与确定性状态流转能力。

---

## 1. 架构定位与中立性底线

- **业务无关性（Zero Business Literals）**：
  核心代码绝对不包含任何特定企业的业务名称、型号代际、专有通讯平台字面量或环境主机指纹。
  由 `tests/test_business_neutrality.py`（AST 代码扫描）与 `tests/test_repo_neutrality.py`（全仓文本扫描）双重门禁强制保证。
- **配置与业务注入**：
  所有业务特定约定（企业术语、专有平台参数、业务模型路由等）均由外部通过 `MemoryConfig`、`SOUL.generic.md` 或 profile 配置文件动态注入。

---

## 2. 逻辑领域架构（7 大领域）

为确保 100% 向后兼容性，`agentcore` 保持清晰扁平的顶层 Python 导入契约（`from agentcore.<module> import ...`），在架构逻辑上划分为 7 大核心领域：

1. **同事制与多智能体组织学 (Colleagues & Multi-Agent Orchestration)**
   - 8 大研发同事岗位契约（`colleague_dev.py`）、工作区物理隔离与单一写者（`dev_runner.py`）、同事注册与网关协同（`colleague_registry.py`, `colleague_gateway_dispatch.py`）。
2. **卡片交互与流式前端 (Cards & Stream Interaction)**
   - 原生 CardKit 2.0 状态机渲染（`cardkit2_renderer.py`）、单在途流式更新（`card_stream.py`）、任务状态卡片解耦桥接（`task_card_bridge.py`）。
3. **交付、事实回投与安全证据 (Delivery, Receipts & Traceability)**
   - 任务完成回执投递调度（`completion_delivery.py`）、终态直投保证（`direct_completion.py`）、交付安全脱敏（`delivery_sanitizer.py`）。
4. **持久任务池与进程监督 (Execution Engine & Supervision)**
   - SQLite WAL 持久任务池（`pool.py`）、执行消费进程（`worker.py`）、Subreaper 进程监管与停止证明（`attempt_supervisor.py`）、插话打断控制（`barge_in_control.py`）。
5. **长期记忆与可塑性治理 (Long-Term Memory Subsystem)**
   - 双路召回与向量存储（`memory/`）、突触可塑性反馈（`memory_plasticity.py`）、容量与卫生治理（`memory_hygiene.py`, `memory_footprint.py`）。
6. **状态图引擎与系统韧性 (State Graph, Liveness & SRE Observability)**
   - 零外部依赖确定性状态图引擎（`state_graph.py`）、存活心跳与假死检测（`liveness.py`, `liveness_daemon.py`）、集中式结构化日志（`structured_logging.py`）。
7. **配置、治理与模型拓扑 (Governance, Profiles & Routing)**
   - 模型多级动态路由（`model_routing.py`）、模型上下文动态探测（`model_context.py`）、单根受管 Profile 规范（`profile_spec.py`）。

完整的 103 个模块归属与职责映射详见：[**`agentcore/PACKAGE_MAP.md`**](PACKAGE_MAP.md)。

---

## 3. 开发与测试守则

1. **零第三方重型依赖**：
   核心执行链路优先基于 Python 3.12+ 标准库实现，轻量依赖按需选用，绝不在核心包引入侵入性重量级框架。
2. **强类型与防御性编码**：
   所有对外函数与类方法必须标注完整的类型提示（`typing` / `__future__.annotations`）。
3. **断言必须伴随验证**：
   修改或新增任何核心功能，必须同步在 `tests/` 编写覆盖正向、边界与逆向异常的测试用例。
