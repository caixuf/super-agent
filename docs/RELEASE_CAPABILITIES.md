# 源码能力面与发版核对

编制日期：2026-11-04。本文按**当前源码**盘点仓库提供了哪些可组装能力，供发版时逐项
核对。它不是「已通过」声明，也不写死任何运行态约定。旧版按发版角色划分的 42 条
核对项及其中的外部卡片 sidecar 约定已随代码整理退役；本版按代码模块重组，条数由
实际能力面决定。

## 0. 口径（先读）

三条边界不能互相替代：

| 边界 | 含义 | 谁负责 |
| :--- | :--- | :--- |
| 源码存在 | 模块在仓库里、有自动化测试覆盖（本文第 1–9 节） | 本仓 |
| 实例启用 | 某个 profile / 服务 / 插件开关真的打开 | 实例部署清单 |
| 运行验收 | 在明确授权的目标上跑通真实模型、真实渠道与业务样本 | 发版验收 |

本文只回答第一条。**当前发版证据与未完成门禁以
[RELEASE_READINESS.md](RELEASE_READINESS.md) 为准**；本文不重新执行运行验收，
也不改变功能开关、兼容组合或生产配置。

每条标注一个证据层级，依据是仓库测试文件的存在性，不是运行验收结论：

| 标记 | 含义 |
| :--- | :--- |
| A | 有仓库自动化测试覆盖并含反例；仍可能是替身或隔离环境 |
| B | 仅隔离 / 合成环境验证，未接真实宿主或渠道 |
| C | 代码存在但缺直接自动化证据，需现场验收 |
| D | 可选或外部依赖，默认不启用，须逐项声明支持 / 关闭 / 不支持 |

**本仓是通用本体**：业务字面量由仓级棘轮门禁 `tests/test_repo_neutrality.py` 拦截，
业务语义、工具、知识与身份一律以叠加层注入。交互与交付由 `agentcore/` 原生链路
承担，仓库不再维护外部卡片 sidecar 集成；相关历史报告只作决策证据留在
`docs/reports/`，不代表当前架构，也不再是发版能力项。

---

## 1. 任务与执行内核

| ID | 能力 | 代码入口 | 层级 | 最小验收出口 |
| :-- | :--- | :--- | :--: | :--- |
| T01 | 持久任务、依赖、租约与结果 outbox | `pool.py` `worker.py` `scheduler.py` | A | 稳定任务号、同请求幂等、跨身份拒绝；提交超时不冒充未接单 |
| T02 | 取消与超时有停止证明 | `attempt_supervisor.py` `session_execution.py` `liveness.py` | C | 先记取消请求，确认本 attempt 及受控后代停止再终结；证明缺失保留未知 |
| T03 | 重启恢复不制造重复副作用 | `pool.py` `worker.py` `completion_backfill.py` | B | 独立进程重启、在途崩溃、旧 attempt 回执均有反例；完成任务不重跑 |
| T04 | 编排、计划门禁与子任务协议 | `pipeline_dag.py` `plan_gate.py` `subagent_protocol.py` | A | 阶段依赖可核对；不可逆阶段按策略拒绝、暂停或另建后续任务 |

## 2. 交付与交互（本仓原生链路）

| ID | 能力 | 代码入口 | 层级 | 最小验收出口 |
| :-- | :--- | :--- | :--: | :--- |
| D01 | 完成态回到提交它的会话 | `completion_delivery.py` `direct_completion.py` `completion_backfill.py` | A | origin 固定绑定；发送与确认分别记账；断线后核对；发送未知不盲重发 |
| D02 | 平台 transport 与送达回读 | `feishu_transport.py` | A | 发送后从服务端回读并比对会话与文本摘要；按键至多一次；凭据不落日志 |
| D03 | 卡片渲染与单在途异步更新 | `cardkit2_renderer.py` `card_stream.py` `card_emit.py` | A | 公开阶段单独展示；最终交付前先停止新进度再 drain；超时或失败显式报错，不当作送达 |
| D04 | 卡片句柄持久化与停滞桥接 | `card_store.py` `task_card.py` `stalled_card_bridge.py` | B | 重启续 sequence；崩溃恢复保留未知状态，不盲目重发 |
| D05 | 文档、附件与图文交付 | `markdown_delivery.py` `deliverable_mode.py` `artifact_retention.py` `document_intent.py` | A | 文本、图表与附件样例实际送达且目标用户可打开；不以生成文件代替交付 |
| D06 | 交付三态语义 | D01–D03 的终态约定 | B | 区分「已接受 / 已渲染 / 已送达」；终态只认 delivered / not_sent / unknown |

> 卡片与完成态投递是**同一个所有者**：一个交互只能有一个生产者。本仓不再维护
> 第二套外部 sidecar 发送者；若未来替换，须在同一次变更里完成迁移与验收。

## 3. 研发同事与派工

| ID | 能力 | 代码入口 | 层级 | 最小验收出口 |
| :-- | :--- | :--- | :--: | :--- |
| C01 | 岗位契约与名册 | `colleague_dev.py` `colleague_registry.py` `colleague_catalog.py` | A | 身份跨任务稳定；岗位、权限、记忆与执行代次可追溯；撤销后不可越权 |
| C02 | Gateway 派工与工具后端 | `colleague_gateway_*.py` `colleague_tool_gateway_backend.py` `colleague_safe_gateway_backend.py` | A | 派单由宿主登记；执行与验收单一写者；名册存在不等于实例就绪 |
| C03 | 只读分析与引用核查 | `analysis_runner.py` `ccp_analysis_model.py` `colleague_readonly_*.py` | A | 报告及引用可回读；路径、输出与并发预算受限；前台退出不丢任务 |
| C04 | 工作流与签收投递 | `colleague_workflow_*.py` `colleague_queue_delivery.py` `colleague_*_delivery.py` | A | 每阶段单一所有者；签收回执可核对；不重复投递 |
| C05 | 受控编码与验收 | `dev_runner.py` `verification_gate.py` `exec_runner.py` `colleague_containment.py` | A | 验收命令取自管理员策略并在模型前冻结；工作树锁贯穿执行与验收；不默认合并或部署 |
| C06 | 对话、历史、插话与人审 | `colleague_dialogue*.py` `hitl_bridge.py` `follow_up_store.py` `barge_in_control.py` | C | 补充绑定任务与版本；区分收到与执行方已应用；审批超时不当同意 |
| C07 | 执行角色双引擎兜底 | `install/worker/dev_exec_runner.py` `toolsets/pi_executor/` | A | 主引擎失败、超时或空输出时改用外部编码执行器，回执写明实际引擎；两引擎都失败须显式失败，不得冒充完成 |

## 4. 记忆

| ID | 能力 | 代码入口 | 层级 | 最小验收出口 |
| :-- | :--- | :--- | :--: | :--- |
| M01 | 长期存储、双路召回、冲突与衰减 | `agentcore/memory/` | A | 写入、去重、冲突、更新及恢复有证据；重启后可读；维护失败不破坏原库 |
| M02 | 新会话自动召回与身份路由 | `plugins/localvec/` `memory_evaluation.py` `profile_overlay.py` | A | 真实新会话命中固定样本；无结果与服务故障分开；身份与代际隔离 |
| M03 | 对话经验受控入库 | `plugins/memory_bridge/` `memory_hygiene.py` `agentcore/learning/` | B | 捕获不阻塞前台；候选经审查后入库；错误准入、冲突、重复和失败可审计 |
| M04 | 团队共享与资源治理 | `colleague_shared_memory.py` `memory_footprint.py` `artifact_retention.py` | A | 个人 / 团队 / 成员授权明确；未授权读写拒绝；撤销生效；会话历史不因共享记忆而共享 |

## 5. 身份、权限与安全边界

| ID | 能力 | 代码入口 | 层级 | 最小验收出口 |
| :-- | :--- | :--- | :--: | :--- |
| S01 | 身份骨架与业务叠加 | `identity/` `profile_overlay.py` | A | 骨架原样在前、注入点唯一、占位符替换干净；现场人工修改不被静默覆盖 |
| S02 | 能力下限与配置基线 | `capability_guard*.py` `baseline_config.py` `preflight.py` | A | 配置按契约变更；缺依赖 / 不兼容有诊断；无错误日志不冒充功能通过 |
| S03 | 执行隔离与写保护 | `safety_envelope.py` `middleware_policy.py` `plugins/write_guard/` `verification_gate.py` | A | 文件、命令、凭据与输出预算受控；工作树和文件锁不冒充安全沙箱 |
| S04 | 审批与不可逆拦截 | `hitl_bridge.py` `plan_gate.py` `colleague_cancel_guard.py` | C | 主账未停止时不得记为已批准；审批超时不当同意；拒绝大小写真值歧义 |

## 6. 可观测与运维

| ID | 能力 | 代码入口 | 层级 | 最小验收出口 |
| :-- | :--- | :--- | :--: | :--- |
| O01 | 活体账本、状态与进度 | `liveness*.py` `plugins/task_status/` `plugins/task_progress/` | A | 区分排队、运行、等待、停滞与未知；进度有事件依据；心跳投影不擅改主账终态 |
| O02 | 告警、巡检与修复决策 | `ops_alerts*.py` `proactive_watch.py` `plugins/capability_alert_outbox/` `failure_mender.py` | C | 区分观察、建议、通知与执行；每步有开关；没有自动执行就不宣称会自愈 |
| O03 | 模型与资源治理 | `model_context.py` `model_routing.py` `ccp_config.py` `process_pressure.py` `tcp_watchdog.py` | A | 记录实际模型及超时 / 并发 / token / 输出预算；路由故障与降级显式 |

## 7. 插件面

`plugins/` 下 13 个插件均为**可选注册**，装了不等于工具面里看得见：

| 插件 | 职责 | 插件 | 职责 |
| :--- | :--- | :--- | :--- |
| `durable_dispatch` | 持久只读派单 | `task_status` | 回答「还在跑吗」 |
| `task_progress` | 长任务单条进度 | `longtask_guard` | 派发前转后台 |
| `localvec` | 每轮自动召回 | `memory_bridge` | 捕获文本与终态判词 |
| `write_guard` | 落盘纪律钩子 | `markdown_delivery` | Markdown 转在线文档 |
| `illustrated_delivery` | 图文建议与投递守卫 | `colleague_catalog` | 岗位名册工具 |
| `colleague_team` | 团队管理（宿主管生命周期） | `capability_alert_outbox` | 能力漂移告警 |
| `local_model_grounding` | 双本地模型辅助 | | |

## 8. 安装、部署与复验

| ID | 能力 | 代码入口 | 层级 | 最小验收出口 |
| :-- | :--- | :--- | :--: | :--- |
| I01 | 可复现正式发布物 | `tools/package_superagent.py` `release.json` | A | 干净提交；文件摘要 / 模式齐全；同输入复现；dirty 候选不能当正式包 |
| I02 | 幂等安装与受管升级 | `install/install.sh` `tools/superagent.py` | A | 干净环境完整安装；失败不留半激活；重装无额外漂移 |
| I03 | 配置、权限与状态保护 | `install/configure_*.py` `install/enable_plugins.py` | A | 现场身份与显式关闭项保留；凭据不打包；状态库不被代码升级覆盖 |
| I04 | 自检、干净室与特性矩阵 | `verify.sh` `docker/` `checks/` | A | 退出码 = 失败项数；缺依赖的 SKIP 不伪装成通过；容器断网、零宿主挂载 |
| I05 | 常驻服务骨架 | `runtime/` `runtime/hermes_*.py` | B | profile 名 / 服务名不写死；服务 active 不等于功能已加载 |

## 9. 身份与技能叠加层

| ID | 能力 | 代码入口 | 层级 | 最小验收出口 |
| :-- | :--- | :--- | :--: | :--- |
| X01 | 纪律骨架与派工角色 | `identity/SOUL.generic.md` `identity/agents/` | A | 八节纪律齐备；角色文件都含回执字段与派工卡；身份真的进入发给模型的 prompt |
| X02 | 技能库与外部执行器 | `skills/` `toolsets/pi_executor` | B | 技能安装后可发现并按预期触发；外部执行器真跑小任务并回读产物 |

## 10. 可选扩展与外部依赖（必须有状态）

| ID | 扩展 | 如何避免成为隐含发版缺口 |
| :-- | :--- | :--- |
| E01 | 夜间复盘与定时作业 | `runtime/autotask/` `runtime/cron/` `tools/autotask/`；声明时区、预算、重试与重复抑制，先手动验通 |
| E02 | 外部执行器与本地模型 | `toolsets/pi_executor` `plugins/local_model_grounding/`；标注依赖与降级，只有目标环境实测后才承诺性能收益 |
| E03 | 代码图谱与文档渲染 | `agentcore/codegraph/` `tools/arch_doc.py`；说明是否随包、如何调用与验收，不把辅助工具当运行主干 |
| E04 | 学习、蒸馏与记忆可塑性 | `agentcore/learning/` `skills/skill-distillation/`；分别说明输入来源、审查、晋升、撤销与消费者 |

可选能力必须逐项写明**支持 / 关闭 / 不支持**，不得留成隐含承诺；缺失可选后端
不得静默变成通过。

### 10.1 本实例的逐项状态（bot1，2026-11-07）

| 扩展 | 状态 | 依据 |
| :--- | :--- | :--- |
| E02 的 `toolsets/pi_executor` | **关闭** | 随包安装但本实例未配置目标执行器；未做真跑回读，不承诺性能收益 |
| E02 的 `plugins/local_model_grounding` | **关闭** | 该插件只做 HTTP 客户端；它需要的 `POST /decide` + `POST /encode` 模型网关脚本在另一台直连网线的机器上（仓库只提供客户端，不提供该网关），且本机没有判别模型。**缺后端就不开**，不静默降级成半开 |
| E01 夜间复盘与定时作业 | **未验收** | 随包能力在位，但本实例未声明时区/预算/重试口径，未做手动验通 |
| E03 代码图谱与文档渲染 | **随包，未做能力面验收** | 属辅助工具，不当运行主干 |
| E04 学习、蒸馏与记忆可塑性 | **关闭** | 记忆侧只启用「捕获 + 召回」；自动写入与策略自动晋升须单独授权与验收 |

判定口径：`plugins.entries.<id>.settings.enabled` 为 true **且** 其依赖后端在同一环境
实测可达，才算该能力开启。仅在 `plugins.enabled` 列表里出现不算。

## 11. 验收口径

每个 ID 在发版报告里记录一行：本版承诺 / 实现证据 / 安装配置证据 /
运行与功能证据 / 验收结论 / 负责人与阻断项。结论只使用
**通过、失败、未验收、不适用**；「不适用」须在范围冻结前说明原因。

硬门槛：

1. 冻结核心、业务适配、实例清单及外部依赖组合，每项有责任人。
2. 全部本版承诺项通过；关键场景跳过、未知或未验收均不算通过。
3. 串身份、权限绕过、未知副作用盲重放、凭据泄漏、数据破坏或回退失败，任一阻断发布。
4. 隔离环境通过新装、升级、幂等及失败回退；不能用源码单测替代。
5. 在明确授权的测试目标完成真实模型、渠道、连续消息、插话、附件及业务样本验收。
6. 生成与版本绑定的发布说明：启用项、关闭项、限制、证据、备份、切换及回退步骤。

`checks/real_link_acceptance.py` 部分场景直接操作临时 Pool，
`checks/deployment_layered_audit.py` 只读进程环境与日志；两者都不能仅凭名称
认定真实渠道插话或真实服务恢复已验通。复用有效部分，缺口逐项补证，不再创建
平行的健康 / 任务主账。
