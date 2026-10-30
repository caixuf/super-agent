# T1/T2 证据一致性门禁（schema 2）

## 结论

\`agentcore/acceptance_evidence.py\` 是平台无关的证据一致性检查器。它已经通过 26 项单元测试，但还没有连接可信运行时采集器，因此不能单独证明真实消息链路通过。

它只回答一个问题：给定一份完整且可信的标准化 capture，里面的消息、会话、任务、attempt 和最终回执是否彼此一致。

它不负责采集日志，也不验证客户端渲染、取消链路、业务结论、记忆质量或用户观感。手工编造的 JSON 仍然可以伪造通过。

## 运行方式

\`\`\`bash
cd agentcore
python3 -m agentcore.acceptance_evidence < evidence.json
\`\`\`

- 通过：输出 \`\"passed\": true\`，退出码 0。
- 失败：输出 \`\"passed\": false\` 和 \`failed_gates\`，退出码 1。
- 缺字段、capture 不完整、来源不可信或数据格式错误：失败关闭。

## capture 最小契约

\`\`\`json
{
  "schema_version": 2,
  "capture": {
    "opened_at": 0.0,
    "closed_at": 10.0,
    "complete": true,
    "clock": "host_monotonic",
    "sources": ["gateway", "queue", "transport"]
  },
  "requests": {},
  "events": []
}
\`\`\`

三个来源缺一不可：

- \`gateway\`：用户入站、admission、turn、真实 tool call/result。
- \`queue\`：task attempt、running probe、状态迁移。
- \`transport\`：最终出站、confirmed delivery、非空 transport receipt。

时间必须来自同一台主机的 monotonic clock。不要拼接不同机器的墙钟时间。

## 固定验收场景

门禁不是可勾选的检查列表，以下三类场景都必须存在：

1. \`same_session_burst\`：同一会话至少三条入站，三条都在第一条结束前到达；每条只 admission 一次、只最终回复一次，并保持会话顺序。
2. \`cross_session\`：两个不同会话的 processing window 必须真实重叠；“两个会话最后都回复了”不算并行证据。
3. \`background_interjection\`：真实 \`durable_submit\` 返回 32 位 task_id 后，普通问题在同一个 running attempt 内到达并完成回复；reply 前后都要有时间窗口不超过 1 秒的 running probe。

后台完成事件不能占用 \`lane=main\` 的用户回合。

## 关键拒绝条件

门禁会拒绝：

- 只在助手文字里提到工具名或任务编号；
- 临时委派编号冒充持久 task_id；
- tool result 与 call_id、turn、session、route 不匹配；
- 同一逻辑消息重复 admission、丢回复、双回复或孤儿回复；
- transport redelivery 的内容或路由发生冲突；
- 回复发生时任务已经 completed，或前后 probe 不是同一 attempt；
- 后台完成通知占用用户主回合；
- 同会话主回合重叠、FIFO 破坏；
- 出站状态为 unknown，或缺少 transport receipt；
- 答案正确但超过每条 request 明确声明的延迟预算；
- expected_text 不一致；
- capture 不完整、事件 ID 重复、时间不是有限数、来源错误。

## 最近验证

\`\`\`bash
cd agentcore
TMPDIR=/dev/shm python3 -m pytest tests/test_acceptance_evidence.py \
  -q --basetemp=/dev/shm/acceptance-evidence-v2
\`\`\`

最近一次结果：26 项通过。

这 26 项只验证门禁自身。真实链路仍必须由可信运行时 exporter 生成 capture，然后运行本门禁；不能把旧日志手工整理成“通过样本”。

## 下一步

先在现有消息入口、持久队列和 transport 回执处加只读 exporter，不改变调度行为。exporter 要在一个 capture 中保留稳定 event ID、session/route/turn、tool call/result、task/attempt 和 transport receipt。

真实场景执行和回滚步骤见业务工作区：

\`docs/handover/2026-10-26_core-execution-guide.md\`

