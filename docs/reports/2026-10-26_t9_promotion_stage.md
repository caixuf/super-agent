# T9 第二阶段：候选晋升（write_fact）+ 写入后召回探针

日期：2026-10-26。状态：实现完成，未提交（按派工要求）。未联网、未写生产记忆库、
未改候选账本、未标记候选为已确认事实。

## 交付文件（新增 2 个 + 本报告）

| 文件 | 作用 |
|---|---|
| `agentcore/learning/promotion.py` | 候选晋升、稳定写入身份、召回探针、跨用户隔离断言 |
| `tests/test_learning_promotion.py` | 17 个离线用例 |
| `docs/reports/2026-10-26_t9_promotion_stage.md` | 本报告（untracked） |

没有修改 `candidates.py` / `retrieval.py` / `learning/__init__.py` / 任何记忆引擎文件。

## API

常量：`PROMOTION_SOURCE="t9_promotion"`；探针状态 `PROBE_HIT="hit"`,
`PROBE_NO_MATCH="no_match"`, `PROBE_ERROR="error"`, `PROBE_SCOPE_LEAK="scope_leak"`。

异常：`PromotionError(CandidateError)`，子类
`CandidateNotPromotable`（状态/证据不允许晋升）、`PromotionWriteFailed`（记忆库拒绝写入）。

```python
promote_candidate(store, candidate_id, *, reviewer, memory_store=None,
                  source="t9_promotion", user_id=None) -> PromotionResult
```

- `PromotionResult`：`candidate_id, fact_id, written, deduped, provenance_match,
  source, reviewer, statement, reason`；`ok = 有 fact_id 且 (written or deduped)`。
- `reviewer` 必填、非空、无换行/制表符（`InvalidCandidate`）；只做溯源，不修改候选。
- 只有 `review_status == "approved"` 且 `evidence_complete` 的候选能晋升；
  pending / rejected / conflicted / needs_human / withdrawn（未知状态）全部抛
  `CandidateNotPromotable`，拒绝发生在打开事实库之前。
- 唯一交接面是 `Candidate.as_fact_proposal()`；载荷若违反
  `confirmed_fact=False` / `requires_memory_write=True` 契约则抛 `PromotionError`。
- 不变式：不调用 `review`/`escalate`，不追加 `candidates.jsonl` / `reviews.jsonl`，
  候选状态与 `is_confirmed_fact=False` 原样保留（测试逐字节比对账本）。

幂等写入：

```python
stable_fact_id(candidate_id) -> "candfact_" + sha256(candidate_id)[:32]
```

写入时传 `fact_id=stable_fact_id(...)`、`subject=candidate.subject or ""`、
`source=source`、`user_id=user_id`，标签包含
`candidate_id, candidate_domain, source_id, source_url, source_hash, retrieved_at,
statement_fingerprint, promotion_reviewer, promotion_source, promotion_stage`。
重试命中记忆库既有 `(user, workspace, subject, statement)` 行 → 返回同一 fact_id、
`written=False, deduped=True, reason="duplicate"`。`provenance_match` 表示既有行的
`candidate_id` 标签确为本次候选（若同语句事实来自别的来源则为 `False`，仍不重复写）。

```python
recall_probe(fact_id, query, *, memory_store=None, user_id=None, ebm=None,
             limit=10, tags=None, check_scope=True) -> RecallProbeResult
```

- 先 `memory_store.flush_background()`（返回值 >0 视为未汇合 → `error`），再
  `recall_facts(query, user_id=..., ebm=..., tags=..., limit=...)`。
- `hit`：结果里出现该 fact_id；`no_match`：召回正常但没有它（带 `empty_reason`）；
  `error`：flush/召回抛错、返回非映射、或 `ok=False`（`error` 字段保留原因）。
- `check_scope=True`（默认）：再用另一个 user_id（`__t9_recall_probe_other__`，
  与 owner 冲突时加 `_2`）召回；同 fact_id 出现即为 `scope_leak`，
  `scope_isolated=False, leaked_to=<user>`。关闭时 `scope_checked=False,
  scope_isolated=None`。
- `RecallProbeResult.ok = status == "hit" and scope_isolated is not False`。
- `memory_store=None` 时延迟 import `agentcore.memory.memory_store`，模块顶层不依赖事实库。

## 验证

```text
$ cd agentcore
$ TMPDIR=/dev/shm python3 -m pytest tests/test_learning_promotion.py -q
17 passed in 0.18s

$ TMPDIR=/dev/shm python3 -m pytest tests/test_learning_candidates.py \
    tests/test_learning_retrieval.py tests/test_import_boundary.py \
    tests/test_business_neutrality.py tests/test_repo_neutrality.py -q
113 passed in 1.15s

$ TMPDIR=/dev/shm python3 -m pytest tests -q
592 passed, 9 skipped in 25.43s
```

9 个 skip 是既有的可选原生绑定缺失（`test_scheduler.py` / `test_worker.py`），与本切片无关。
用例全部使用 `tmp_path` 候选库 + conftest 的确定性嵌入临时记忆库，不触生产数据。

## 覆盖清单

- approved 晋升一次：`written=True/deduped=False`，fact_id 稳定，标签/来源/审查者完整；
- 第二次调用：`written=False/deduped=True`，同 fact_id，无第二条事实；
- 晋升不追加候选/审查账本，不把候选标成已确认事实；
- pending / rejected / conflicted / withdrawn（假库）→ `CandidateNotPromotable`；
- 未知候选 → `UnknownCandidate`；空 reviewer → `InvalidCandidate`；
- 记忆库 `ok=False`、抛异常、缺 `fact_id` → `PromotionWriteFailed`；
- 探针 hit（不手动 flush 也能命中，证明探针自己汇合后台线程）；
- 探针 no_match；flush 异常 / 召回异常 → error；模拟泄漏库 → scope_leak；
- 真实库跨用户隔离：owner 命中、`intruder-user` 空结果、`scope_isolated=True`；
- `check_scope=False` 跳过隔离检查；`stable_fact_id` 确定性/长度/空值拒绝。

## 剩余（本阶段明确未做）

- 真实审查界面/通知与审批人身份体系（`reviewer` 目前只做非空/格式校验并入库溯源；
  角色鉴权、审批工单、撤销/撤回流均未接入）。
- 真实抽取器：候选仍只来自 `retrieval.ingest_fetched` 的“已抓取快照”，无抓取调度、
  无 LLM 抽取、无自动生成 statement。
- 命中后的置信度强化（synapse/reinforce）、探针结果回写候选、失败告警接入 T8 AlertOutbox。
- 语义冲突检测、`resolve_conflict=True` 的晋升策略（当前刻意不开启自动取代）。
