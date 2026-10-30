# T-B 可信 capture 录制器（手册第五节「可信证据」）· 测试报告

日期：2026-10-26
执行人：Agent（远端宿主 现场，`agentcore`）
上游依据：`2026-10-26_core-execution-guide.md` 第五节「可信证据最少字段」与第十二节第 5 步
结论：**导出侧代码完成并通过 9 组用例（含两次突变验证）**；尚未接进网关运行时，因此**现场仍出不了可信 capture**。

## 一、为什么需要这一件

`acceptance_evidence.py` 只能审计「你交给它的一份 capture」。它自己在文档里就写明：
手工伪造的 JSON 仍可能骗过它，所以 capture 必须由可信运行时导出。

缺的就是这个导出器：没人录制，三场验收就没有证据源。而「事后从日志重建」是不可行的——
证据要求 `clock: host_monotonic`，墙钟日志里没有单调时钟，事后拼出来的时间戳是在制造
它本该证明的那个性质。

## 二、改动

| 文件 | 改动 |
| :--- | :--- |
| `agentcore/acceptance_capture.py`（新） | `Recorder`：单调时钟录制 + 逐行落盘 + `seal()` 出 schema-2 记录；`seal_from_file()` 崩溃取证；`python3 -m agentcore.acceptance_capture <file>` CLI |
| `agentcore/acceptance_evidence.py` | 把「事件类型→来源」映射提成 `EVENT_SOURCES`、三来源提成 `REQUIRED_SOURCES`，审计器内部改用常量（行为不变） |
| `tests/test_acceptance_capture.py`（新） | 9 组用例，含一份完整三场景轨迹 |

改成单一事实源的理由：录制器与审计器如果各写一份映射，两个文件迟早漂移，
结果是「录制器认为合法的 capture 被审计器判非法」，而且只在真实验收当天才发现。

## 三、录制器的失败关闭规则（都在录制时执行，不留给审计器）

- 未知事件类型 → 拒绝；
- 调用方传 `source` → 拒绝（来源由类型推导，不许自证来源）；
- 事件 id 重复 → 拒绝（宁可当场炸，不封一份有重号的 capture）；
- 时间戳非有限数、或早于 capture 开窗 → 拒绝；
- 已知丢事件 → `gap(reason)`，封出来 `complete: False`，审计器必然判 `incomplete_capture`；
- 已封窗后再录/再声明预期 → `RuntimeError`；
- 进程中途崩溃（没有 `__sealed` 行）→ `seal_from_file` 报 `complete: False`，不是「没发生」。

## 四、命令与结果

```
$ TMPDIR=/dev/shm python3 -m pytest tests/test_acceptance_capture.py tests/test_acceptance_evidence.py -q
35 passed in 0.25s

$ TMPDIR=/dev/shm python3 -m pytest tests/ -q
366 passed, 9 skipped in 32.90s
```

CLI 冒烟（真实单调时钟、真实文件）：

```
$ python3 -m agentcore.acceptance_capture /tmp/.../c.ndjson
exit: 0
{ "schema_version": 2,
  "capture": { "opened_at": 5383189.1498..., "closed_at": 5383189.1500..., "complete": true,
               "clock": "host_monotonic", "sources": ["gateway", ...] }, ... }
```

## 五、关键证据：录制器的输出形状 = 审计契约

`test_recorded_three_scenario_trace_passes_the_auditor` 用假单调时钟录一份完整轨迹
（同会话三连发 A1/A2/A3、两会话交叉、真实 `durable_submit` + running 期内插话），
交给 `evaluate()` 判 **passed: True**，并核对 `durable_task_ids`、`logical_inbound_count`。

这条用例证明的是「API 形状能用」，不是「现场能跑」——手工拼 JSON 也能通过审计器。

### 突变验证（证明测试不是摆设）

| 突变 | 结果 |
| :--- | :--- |
| 删掉到达探针（`position=arrival`） | 3 组 RED（`reply_while_task_running`） |
| 把插话回合标成 `origin=completion` + `lane=main` | 3 组 RED（`completion_occupies_main_turn`） |
| 还原 | 9 组 GREEN |

两次突变都由审计器正确拒绝，说明这份轨迹确实逐条满足验收门禁，不是「恰好通过」。

## 六、未验 / 边界

1. **没有接进网关运行时**：现在没有任何生产代码调用 `Recorder`。要真正产出 capture，需要在
   入站、回合起止、tool call/result、transport receipt、任务探针这些点上调用录制器，
   这些点分布在 `run_notifications.py` / `session_authority.py` / `pool` 侧，属于 G2 那批补丁的范围。
2. **transport receipt 的可信来源未定**：`message_out` 要求 `delivery_status=confirmed` +
   非空 `transport_receipt`。推送型 IM 适配器当前的返回结构是否提供稳定的 receipt id 没有验证，
   这是接录制器时的第一个待确认项；拿不到就只能如实录 `delivery_status` 的其它取值，
   而那会让审计判红——宁可红，不伪造。
3. **任务探针（before/after/arrival）需要 1 秒内的窗口**：录制器只能如实记录时刻，
   能不能拿到这么密的探针取决于 worker 侧是否有可用的运行时接口，未验证。
4. 未做跨进程/跨主机时钟；多进程共同录制同一窗口不在支持范围内（设计上就反对）。

## 七、回滚

`git revert` 本次 commit。`acceptance_evidence.py` 只是把内联字典提成常量，回滚后行为不变。

## 八、下一步（只写一个）

在 G2 的补丁里同时埋 `Recorder` 调用点（入站、回合起止、`durable_submit` 的 call/result、
`message_out`），用一次隔离 profile 的普通问答先导出第一份**真实** capture，
确认 `complete: True` 且 `evaluate()` 只因业务预期（而非字段缺失）判红。
