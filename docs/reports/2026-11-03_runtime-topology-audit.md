# 运行拓扑只读核对

核对日期：2026-11-03（Asia/Shanghai）。这是当时现场的快照，不是持续监控，
也不是部署或能力验收。仅检查用户服务状态、进程绑定和配置白名单字段；
没有读取队列任务正文、记忆正文或凭据，没有更改配置、重启服务或发送消息。

## 当前链路

| 部件 | 实际绑定 | 证据的限制 |
| --- | --- | --- |
| Gateway | `hermes-gateway.service` 为 active/running；`HERMES_HOME` 指向 `~/.hermes`，启动器引用该 home 下的宿主 checkout | 服务存活不证明模型、渠道健康，也不证明当前进程已加载磁盘上的每项改动 |
| 只读 Worker | `agentcore-worker@default.service` 为 active/running；`agentcore.pool` 指向 `~/.hermes/var/durable-queue` | 与有效派单配置的队列一致，但未执行真实模型任务或取消验收 |
| runner | 固定 argv 为 Python + `agentcore/analysis_runner.py` + 管理员 policy/model-adapter 参数 | 是已有只读路径，不是已接通的通用研发同事；未调用模型 adapter |
| 记忆服务 | `agentcore-memory@default.service` 为 active/running；固定 profile `default` | user/agent/spool 与有效 memory-bridge 配置逐项一致，仅报告相等性，不记录身份值；不是多用户隔离证明 |
| 卡片 sidecar | `hermes-feishu-card-sidecar.service` 为 active/running，使用记录对应的本地修补 HFC 环境 | 无真实发送或客户端回读，不能据此解除上游 pin 的 hold |

Worker 和记忆服务的 `PYTHONPATH` 仍包含当前工作区与管理员配置目录，
尚未绑定本轮构建的不可变受管候选。不能将“未重启”解释成工作区修改
绝不会影响未来子进程；迁移前须明确版本绑定与回退点，不在运行工作区
原地施加宿主补丁。

## 有效配置与同名目录差异

- Gateway 的有效配置是 `~/.hermes/config.yaml`，不是
  `~/.hermes/profiles/default/config.yaml`。两者是不同文件，插件开关及
  settings 也不同；服务实例的 `default` 名称不能决定实际配置来源。
- 两份配置均未声明 `superagent.frontdesk`。现场宿主检查不到新权限门禁
  标记及两份 frontdesk helper；因此不能声称“薄前台已生产启用”。
- 有效 durable-dispatch 配置启用自动完成通知与 direct delivery，transport
  指向工作区的 `runtime/hermes_completion_transport.py`；task-status 与
  durable-dispatch 使用同一 liveness 路径。
- 有效配置中 task-progress 已禁用，HFC 插件启用；同名 profile 配置中的
  task-progress 则启用。仅凭 profile 文件修改不能证明有效生产者已改变。
  此检查未穷尽所有插件钩子，不能把这项配置证据提升为运行时唯一发送证明。
- `require_delivery_route` 当前显式为 false。这允许没有可投递 route 的提交，
  **不表示每条当前 IM 请求都缺 route**。目标闭环须在明确配置目标后要求
  可寻址来源，并验证拒绝不可投递提交；没有扫描、修补或重放历史任务。
- 记忆桥固定 user/agent，服务主体一致，说明当前是 single-principal 链路。
  不能从 profile/session 存在推导出多用户长期记忆隔离。

## 切换之前必须补齐

1. 明确目标 home/profile 与灰度会话，备份有效配置、已绑定版本和发送所有者；
   不把同名目录当作同一运行配置。
2. 用完整候选及受管路径验证宿主启动与依赖；确定 Worker/记忆服务的不可变
   版本绑定和回退步骤，不把 doctor 的文件校验当作启动证明。
3. 单独启用前台策略和必需 route，先证明接单结束、后台未完成时的新消息、
   跨身份拒绝、重启恢复与唯一出口；避免开始第二套卡片生产者。
4. 真实渠道外发、服务切换/重启及生产记忆操作在明确目标与授权窗口后执行。
   研发 containment、停止证明和记忆准入质量仍按发版门禁独立核验。

本报告没有实施上述切换，也不授权自动消费生产队列或记忆积压。
