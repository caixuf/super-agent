# C 线：T8 能力漂移告警回执

## 结论

已在通用仓新增离线 T8 episode 与可靠投递机制。漂移判断仍由调用方已有巡检器提供；本实现只记录结果、维护漂移 episode，并把通知事件写入与任务队列共用 SQLite 文件中的 C 独占表。它不会触碰任务 `jobs`、任务回执 `outbox`、配置或外部消息平台。

## 文件

- `agentcore/ops_alerts.py`：episode 状态、持久告警 outbox、租约认领和 token 确认、单条投递接口。
- `plugins/capability_alert_outbox/__init__.py`：向现有巡检器提供 `record_inspection` 接入函数，以及投递入口。
- `plugins/capability_alert_outbox/plugin.yaml`：通用插件元数据；无平台目标和自动发送 hook。
- `tests/test_ops_alert_outbox.py`：离线验证。

告警唯一状态位于 `ops_alert_episodes` 与 `ops_alert_outbox`。同 profile/scope 的首次漂移原子生成事件，重复漂移沿用活动 episode；健康观察关闭 episode，下一轮漂移获得递增 generation 和新事件 ID。事件携带期望值、观测值、配置是否已落盘、运行时是否已加载。发送端以 event_id 为幂等键。通知适配器未配置时明确返回 `unavailable`，不会认领或丢弃事件。

lease 与 ack 复用已有 Pool 数据库连接及事务方式，但使用 `ops_alert_*` 独立表和专属 claim/ack 方法，确保任务 worker 的 `claim_receipt()` 看不到告警。只有发送器返回明确的 `DeliveryReceipt(accepted=True, event_id=<本事件ID>, receipt_id=<非空>)` 或等价字典时才确认，并把 `receipt_id` 持久化。明确拒绝、`False`、`None`、缺字段、错误 event ID 和发送异常都不 ack；不确定结果保留事件，租约过期后重试。租约必须是有限正数，拒绝 NaN、正负无穷、零和负数。旧 token 不能确认新租约。若目标平台不支持幂等键，发送成功而本地 ack 前崩溃仍可能导致重复，因此保证为至少一次，而非绝对只发一次。

## 局部验证

- `<hermes-install>/.venv/bin/python -m pytest tests/test_ops_alert_outbox.py -q`：**FAIL**，该隔离 Python 3.14 环境没有 pytest 模块。
- 早期版本 `python3 -m pytest tests/test_ops_alert_outbox.py -q` 的 **8 passed** 不作为当前实现验收；root 审查发现发送回执未 fail-closed。
- 修订版首次 `/dev/shm` 回归为 **22 passed / 1 failed**：失败是并发测试未传新增的回执参数；修正后跑出 23 passed。随后再把相同回执校验加到公开 ack 边界，并新增直接 ack 反例。最终命令 `TMPDIR=/dev/shm PYTHONDONTWRITEBYTECODE=1 python3 -m pytest --basetemp=/dev/shm/agentcore-c-outbox-failclosed-20261026-03 -p no:cacheprovider tests/test_ops_alert_outbox.py -q` 实测 **24 passed in 0.20s**，退出码 0。
- 新增反例覆盖 `False`、`None`、明确拒绝、错误/缺失 receipt 字段、发送异常、成功发送后 ack 前崩溃；所有未获明确接受回执的路径都保留待发事件。租约反例覆盖零、负数、NaN、正负无穷、布尔和非数字。

修订版测试只使用 `/dev/shm` 下的 pytest 临时目录、真实本地 Pool/SQLite 与假发送器，没有模型或外部通知，也不触碰生产队列。测试不是生产适配器验收。

## 未覆盖与下一步

- 插件尚未安装到现役 profile，也未连接现场巡检器和通知目标；没有真实告警出站证据，T8 用户验收仍待执行。
- 需要在现有权威巡检结果产生处调用 `record_inspection`，并由一个受控发送进程注入真实适配器；真实投递须由 root 按已授权流程验收。
- 当前禁止提前做 T6 收口、安装器修改、Docker 验收或发布操作；这些在 T8/T9 等前置功能收齐后再按任务板七条判据最后执行。
