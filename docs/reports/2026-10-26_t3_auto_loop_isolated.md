# T3 隔离最小闭环：决策 → 写入 → 重启 → 召回

日期：2026-10-26
仓库：`agentcore`（master）
性质：隔离测试，不触碰生产记忆、不接 Hermes 运行时。

## 改动

`agentcore/memory/memory_trigger.py::run`：

- 新增 `turn_status: str = "completed"`：非 completed 的回合（failed/interrupted）直接
  `skipped=True, reason="turn_not_completed"`，不进入抽取。
- 抽取前显式检查 `get_config().resolve_llm()`：不可用时返回
  `ok=False, reason="provider_unavailable", retryable=True`，不再伪装成
  `no_fact_extracted` 的空成功。

新增 `tests/test_memory_auto_loop.py`。

## 验证

```bash
cd agentcore
TMPDIR=/dev/shm python3 -m pytest \
  tests/test_memory_engine.py tests/test_memory_evaluation.py tests/test_memory_auto_loop.py \
  -q --basetemp=/dev/shm/t3-memory
```

结果：**23 passed**。

| 场景 | 判据 |
| :--- | :--- |
| 明确决策写入 | `run(...).written == 1`，fact statement 可召回 |
| 同轮重复 | 第二次 `written == 0, reason == duplicate`，库里仍 1 条 |
| 重启后跨会话召回 | 重新 `configure` 同一 data_dir，`recall_facts` 仍命中 |
| 抽取器不可用 | `ok=False, reason=provider_unavailable, retryable=True` |
| 失败回合 | `skipped=True, reason=turn_not_completed`，不写入 |

## 未做

- 未接 Hermes `post_llm_call / pre_llm_call` 钩子，未接 T2 worker。
- 未跑生产记忆基线；需要脱敏口径。
- 未验证真实模型抽取质量、冲突裁决、跨 profile 隔离。

下一步：把 `run()` 的持久事件入队接到 T2 worker，并做 pre-LLM 召回注入。
