# 2026-10-26 计划补齐

授权：用户要求补齐现有计划。生产仿真仍唯一串行入口；不重启网关、不发送外部消息。

## 7 并行切片
- root：T2 durable worker、runtime 模板、共享集成与统一验证提交。
- memory：记忆自动化新增模块 / test_memory_automation.py / memory 回执，不改生产库。
- delivery：arch_doc.py / test_arch_doc.py / T5 新补丁与 delivery 回执，不改在线 Hermes。
- operations：新增 ops/learning 工具 / test_ops_learning.py / ops 回执，不发告警。
- 完成条件：实际代码、可复现验证、未覆盖项分别记录；活链路未测一律待验收。

## 收口与证据
- 用户澄清以指导文档为主，随后允许核心执行层继续；其余切片均停在文档，无生产修改。
- T2 核心新增 agentcore/pool.py 与 runtime/agentcore-worker@.service；未启用服务、未接模型 runner 或消息工具。
- 调度机制：本地 SQLite WAL、原子认领、依赖、租约、继承进程锁、attempt fencing、幂等键、取消/超时、结构化结果 outbox；默认禁止不安全重放。
- tests/test_pool.py：6 PASS；与中立门禁合计19 PASS。
- bash verify.sh：82 PASS、7个shell静态通过、安装器dry-run退出码0。未重跑Docker与在线消息链路。
- T3/T7、T4/T5、T6/T8/T9详细指导位于 docs/reports/2026-10-26_{memory_automation,delivery,ops_learning}.md。
- 用户验收：全部待确认。新核心仍需宿主适配、独立profile、预算与结果发送去重接线；不能称生产上线。
