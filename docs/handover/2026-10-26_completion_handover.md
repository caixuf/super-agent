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


## FlowCoro 核心接入续作

用户明确授权核心实现由本会话完成。切片：root负责持久队列、worker整合、运行说明；
operations负责scheduler与双后端测试；delivery负责恢复审查和回归；memory负责隔离构建原生绑定。
未提交或改动其它并行工作的write_guard文件。

- 新增 agentcore/scheduler.py：有界并发、事件等待、协作取消、异常保留、显式原生后端。
- 新增 agentcore/worker.py：持久认领与调度对接，停止接单后drain。
- 新增 exec_runner.py：新进程应用单文件预算；输出预算退出后再次核查。
- 持久队列新增 Linux孤儿身份核对、超时/取消回收，以及结果outbox租约与token确认。
- 修复依赖generator被耗尽、同幂等键不同重放策略未拒绝、准备目录失败泄露锁等问题。
- 正式操作说明：docs/POOL_RUNTIME.md。未接生产模型工具、未发消息、未启停服务。
- 进程组不是任意代码安全沙箱；逃逸/daemonized子进程需per-attempt cgroup/container。
- 原生上游验证：FlowCoro aaac885e673e01b6f21037a47930ef744668d26b，绑定4.0.0，官方Python测试33 PASS。
- 原生绑定为隔离构建产物，不是已安装生产依赖；必须按部署解释器重新核验。


### 最终统一验证

- `TMPDIR=<本机tmpfs临时目录> PYTHONPATH=/tmp/flowcoro-verify-aHSWNK/build/python bash verify.sh`：退出码0；7个shell静态通过，当前工作树137项测试全部通过（无skip），安装dry-run退出码0。
- 137是当时完整工作树计数，含其它并行任务未提交测试；本提交未包含write_guard插件及测试。
- 原生路径包括真实worker→runner→回执，不只是包装层测试；上游绑定官方测试另有33 PASS。
- 第一次磁盘繁忙联测发现SQLite并行心跳OperationalError，已以本进程事务锁、原子续租和低频心跳修复；高磁盘负载下另一次CLI测试仍触及测试自身20秒超时，不能宣称生产磁盘容量已通过压测。
- 最终tmpfs回归验证事务、进程重启、取消、输出/文件预算、进程认领、token回执等逻辑；未测机器断电、跨机共享存储、真实消息投递、费用预算或生产模型任务。
- 运行与接线指导在 docs/POOL_RUNTIME.md；生产网关、记忆库、消息平台均未改动。
