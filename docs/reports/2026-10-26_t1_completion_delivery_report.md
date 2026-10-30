# T-C 后台完成回执的投递消费者（手册 G2 的 agentcore 半边）· 测试报告

日期：2026-10-26
执行人：Agent（远端宿主 现场，`agentcore`）
上游依据：`2026-10-26_core-execution-guide.md` 第四节第 3 条、第五节场景 C
结论：**完成回执的投递消费者已实现并 21 组用例通过**；宿主侧的「不经回合发送」原语仍未做，
所以后台完成目前**还不会真的发到聊天窗口**——这一条必须说清楚，不能算完成。

## 一、先纠正一个现场事实

改之前先查了「谁消费 `pool.outbox`」：答案是**没有人**。
`claim_receipt()` / `acknowledge()` 除了 `pool.py` 自己的 CLI 之外，全仓无调用点。

也就是说：durable 任务跑完之后，回执一直躺在 `outbox` 里，**用户不会收到任何通知**，
只能自己再发一句 `durable_status` 去查。这解释了为什么「后台任务完成事件」在真实链路里
看不到消息——它压根没有被投递过，而不是「被合并进了主回合」。

这条与手册第四节第 3 条的描述略有出入（手册说「改为先写记录再由 outbox/CardEmitter 投递」），
两者的共同点是：**投递侧是缺的**。本任务补的就是这一侧 agentcore 能负责的部分。

## 二、改动

| 文件 | 改动 |
| :--- | :--- |
| `agentcore/completion_delivery.py`（新） | `deliver_once` / `deliver_pending` / `build_message` / `inspect_pending`（只读干跑）/ CLI |
| `agentcore/pool.py` | `outbox` 新增 `receipt_id`、`delivered` 两列（幂等迁移）；`acknowledge(..., receipt_id=)` 记录回执；新增 `delivery_record(job)` |
| `tests/test_completion_delivery.py`（新） | 21 组用例 |

**复用而非重写**：回执校验直接 import `agentcore.ops_alerts._receipt_status` 与 `DeliveryReceipt`
（T8 已经定型的语义：`accepted is True` + event_id 匹配 + 非空 receipt_id 才算成功）。
两份实现会漂移，而这类漂移的后果是「假 ACK」。

## 三、四条硬规则（每条都对应一个具体的翻车方式）

| 规则 | 不这么做会怎样 |
| :--- | :--- |
| 地址只取 `jobs.origin`（提交时持久化的归属） | 用「当前活跃会话」回填 → 报告发进别人的聊天窗口 |
| 无归属的任务报 `unowned` 且**不 ACK、不投递** | 猜一个地址发出去，比不发更糟 |
| 只有 `DeliveryReceipt(accepted=True, event_id=<本任务>, receipt_id=非空)` 才 ACK | 用「返回 True / 无异常 / 有 dict」当成功 → 消息没发出去却标记已送达 |
| 投递不改 `jobs.status` | 投递失败被写成任务失败，或反之，队列不再是唯一事实源 |

另外明确写进 docstring 的边界：**只能承诺 at-least-once**。发送成功但 ACK 丢失时，
下一轮会重发（idempotency key = task_id）。这不是缺陷，是如实承诺。

## 四、命令与结果

```
$ TMPDIR=/dev/shm python3 -m pytest tests/test_completion_delivery.py -q
21 passed in 0.21s

$ TMPDIR=/dev/shm python3 -m pytest tests/ -q
387 passed, 9 skipped in 27.09s

$ python3 -m agentcore.completion_delivery --root ~/.local/state/agentcore/analysis-queue --limit 5
[]        # 真实队列当前没有待投递回执；该命令只读，不 claim、不 ack
```

## 五、用例覆盖（21 组）

| 组 | 守的性质 |
| :--- | :--- |
| 正常投递 | 地址取 origin、ACK 落 `receipt_id`/`delivered`、二次 drain 不重发 |
| 无归属任务 | 报 `unowned`、不投递、不 ACK、仍在 `receipts()` 里可见 |
| 不改任务状态 | 投递前后 `pool.status(job)` 完全一致 |
| 显式拒绝 | `delivery_rejected`，不 ACK |
| 未证明的接受 ×4 | 空 receipt_id／错 event_id／缺字段／`True`／`None`／`[]` → 全判 `delivery_unknown`，不 ACK |
| 传输异常 | `delivery_unknown` + 异常类型，租约到期后重发一次（at-least-once 实证） |
| ACK 丢失 | `ack_unknown` + 回执 id，恢复后重发并最终 ACK |
| 并发两消费者 | 恰好一个 `delivered`、一个 `empty`，总发送次数 = 1 |
| 未配置传输 | `unavailable`，不泄露任何业务内容 |
| drain 语义 | 连续两条都投递；被卡住的头只尝试一次就停（不空转） |
| 摘要截断 | 超长报告截到 limit 并标注 truncated，不把几 MB 粘进聊天 |
| 失败任务 | `failed` 也投递，摘要取 `reason` |
| 畸形回执 | `invalid_receipt`，不投递不 ACK |
| 回执 id 校验 | 空白 receipt_id 直接 `ValueError` |
| 干跑 | `inspect_pending` 列出地址但不 claim/ack，真消费者仍能取到 |

### 突变验证（证明用例有牙齿）

| 突变 | 结果 |
| :--- | :--- |
| 去掉回执校验，一律 ACK | **9 组 RED** |
| 归属缺失时回落到「当前会话」 | `test_task_without_origin_is_refused_and_left_pending` **RED** |
| 还原 | 21 组 GREEN |

## 六、未验 / 边界（这是本报告最重要的部分）

1. **宿主侧发送原语仍未做**：`Transport` 是注入的接口，生产上谁来 `deliver` 还没定。
   在 Hermes 里给推送型适配器做「不经 LLM 回合直接发送」需要改动
   `gateway/session_automation.py` 与 `run_notifications.py`，属于需要维护窗口的补丁（G2b），
   **本次没有写、没有应用、没有重启网关**。
2. **没有接进任何运行时**：没有守护进程/线程在调 `deliver_pending`。当前状态是「消费者造好了，
   没人点火」。接进去属于部署动作，需要窗口。
3. **transport receipt 的字段来源未验证**：适配器是否返回稳定 receipt id 没查过。
   拿不到就必须如实报 `delivery_unknown`，不能用「发送函数没抛异常」代替回执。
4. **`lease` 默认 60s 与 worker 心跳的关系未验证**：若一次投递慢于租约，可能出现第二个消费者
   同时投递同一条（重复消息）。这是 at-least-once 的已知放大点，没有做过真实网络延迟下的压测。
5. **未做真实聊天窗口的目视验收**：本报告没有任何「用户真的看到了消息」的证据。

## 七、回滚

`git revert` 本次 commit。`outbox` 多出的 `receipt_id`/`delivered` 是加列，
回滚后旧代码照常 `acknowledge(job, token)`，两列留空即可，不需要 `DROP COLUMN`。

## 八、下一步（只写一个）

在 G2b 补丁里给出第一个 `Transport` 实现（推送型适配器的直接发送），
然后把 `deliver_pending` 挂到 worker 主循环旁边，用一条真实任务观察
「`outbox` 待投递数降到 0 且 `delivery_record.receipt_id` 非空」。
在此之前，后台完成对用户仍然是静默的。
