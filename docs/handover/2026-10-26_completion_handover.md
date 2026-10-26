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

## 持久派工与失败分析同事接线

用户授权推进Hermes接入与真实失败分析。先交付隔离可审阅实现，生产启用作为单独验收点。
切片：root负责宿主接线、配置样例和集成验收；runner负责只读证据分析runner；plugin负责Hermes插件与作用域校验；review负责接口审查与集成测试。全部禁止读取或输出凭据、修改其它并行任务文件。


### 接线实现与验收回执

- 新增durable_dispatch插件，三个公开工具通过Hermes registry注册；owner只来自可信dispatcher session_id和profile。
- 新增analysis_runner，只读有界文件快照、管理员model_options、逐条原文引用检查与原子报告产物；不提供模型工具执行。
- 新增dispatch_pipeline集成测试，覆盖调用方重建、异会话拒绝、真实子进程runner、伪造证据失败、结果outbox确认。
- 已修短文件默认end_line越界，插件与runner默认来源数统一8。
- 真实模型探针：合成日志，task=783d05fd4d0e46cbaac5b15fd32767fa，attempt=6b80dda72daf4b3b848d442540bb56e3；FlowCoro worker完成一次，三条claim引用均匹配快照。结论明确区分时间顺序与因果，verification=citation_traceability_only。
- 探针产物：/tmp/agentcore-live-analysis-hzhz_2p5/queue/attempts/6b80dda72daf4b3b848d442540bb56e3/{report.json,report.md,manifest.json}。关键结果写入本回执，不以/tmp作为唯一记忆。
- 统一验证：TMPDIR使用本机tmpfs，PYTHONPATH含真实FlowCoro绑定，bash verify.sh退出码0，当前完整工作树210项通过（无skip），7个shell静态通过、安装器dry-run退出码0。工作树计数包含其它并行任务；本提交不含其write_guard、卡片或README改动。
- 专项指导：docs/DISPATCH_ANALYSIS.md、plugins/durable_dispatch/README.md及两份专项报告。
- 尚未验收：真实业务案例准确性、主模型提交后及时终轮、真实平台状态查询、自动回填与同卡到终。当前插件工具说明要求结束turn，未强制终轮。
- 生产插件、配置与服务未启用；独立配置审阅包已在宿主业务仓落盘。下一步先审阅最小来源目录，再在新会话完成真人验收。

## T4 用户追加：效果与合适时机

用户明确要求T4由本会话接手，不能只修截图，要覆盖架构介绍、机制讲解和计划等语义场景并视觉验收。
切片：render负责arch_doc渲染与机械质量；trigger负责独立语义建议/钩子与测试；examples负责业务架构取证和三类内容样例；root负责技能政策、跨链集成、视觉校样和最终收口。源代码共享件不并改，生产服务不重启。

## 最终收口要求（用户追加，必须最后执行）

全部功能完成后，通用本体仓必须收齐实现、插件、技能、依赖、接线清单和一键部署入口，新机可复现同等能力；用Docker干净环境实际验证。此项是最后工作，当前不提前打包或跑Docker。
最终验收必须覆盖功能实际可用：派工与恢复、模型分析、记忆写入/召回与隔离、图文触发及产物、消息交付、巡检告警、候选学习等最终已承诺能力。仅import成功或安装退出码0不够。外部账号与秘密由部署配置注入，不能烘进镜像；宿主业务叠加有完整清单与校验，不得靠未入仓的个人目录才能接线。


### T4 集成收口

写作标准、语义建议、真实浏览器完整分页和校样已串接。三类样例共6页已逐页看图；真实模型14组交付选择全部符合预期。检查器自身4例中两个负例必须失败，已通过。
已更新并备份现场技能，仅启用illustrated-delivery钩子。网关确认pre_llm_call已加载，PID保持3381565；其它配置经语义审计保持，公共启用API只对插件列表做排序及添加本项。未重启服务或发送外部消息。
用户要求一个一个推进；当前只收尾T4，其它核心项仍保持原进度。完整功能收齐与Docker干净部署最后做。
逐项证据见docs/reports/2026-10-26_t4_integrated.md。真实聊天是否每次正确执行与用户观感仍待持续验收，不把建议器当成强制渲染器。

最终verify.sh退出码0：当前工作树267项通过无skip，9个shell语法通过、安装器dry-run通过；Docker按用户要求留最后。
