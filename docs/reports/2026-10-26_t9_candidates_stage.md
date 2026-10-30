# T9 第一阶段：候选知识 schema + 逐字引文锚定

日期：2026-10-26。状态：实现完成，未提交（按派工要求）。未联网、未起定时器、未写正式记忆。

## 交付文件（新增，3 个）

| 文件 | 作用 |
|---|---|
| `agentcore/learning/__init__.py` | 候选层公开面（数据类 / 状态 / 异常 / 纯函数） |
| `agentcore/learning/candidates.py` | 候选 schema、内容寻址快照、来源去重、审查门禁 |
| `tests/test_learning_candidates.py` | 40 个离线用例 |

## API

数据类：

- `Candidate`：`candidate_id, statement, literal_quote, source_url, source_hash, retrieved_at,
  domain, review_status, reviewer, reviewed_at`（T9 必备字段）+ `subject, source_id, author,
  review_note`。`scope` 属性等于 `domain`（T9 字段表写作 domain/scope，同一维度）。
  `is_confirmed_fact` 恒为 `False`；`as_fact_proposal()` 只在 `approved` 时返回载荷，
  且载荷带 `confirmed_fact=False, requires_memory_write=True`。
- `SourceRecord`：`source_id, source_url, source_hash, retrieved_at, byte_size`；原文落在
  `<root>/sources/<sha256>.txt`（内容寻址）。
- `IngestResult(candidate, created, source_created, snapshot_created)`。
- `GroundingReport(candidate_id, ok, source_present, snapshot_present, hash_matches,
  quote_grounded, reasons)`。

`CandidateStore(root, *, clock=None)`：

- `ingest(*, statement, literal_quote, source_url, retrieved_at, domain, snapshot,
  source_hash=None, subject=None, author=None, review_status="pending")`
- `get(candidate_id)` / `list(*, status=None, domain=None)` / `__len__`
- `sources()` / `source(source_id)` / `snapshot_path(source_id)` / `snapshot_text(source_id)`
- `review(candidate_id, *, reviewer, decision, note="", now=None)`（approve / reject）
- `escalate(candidate_id, *, reviewer, reason, now=None)`
- `recheck(candidate_id)` / `attempts(candidate_id=None)`

状态：`pending / approved / rejected / conflicted / needs_human`；`approved`/`rejected` 为终态。
异常：`CandidateError` 及其子类 `InvalidCandidate, QuoteNotGrounded, EvidenceMismatch,
UnknownCandidate, UnknownSource, ReviewerError, ReviewerNotIndependent,
AutoApprovalForbidden, ReviewBlocked`。

纯函数：`quote_grounded`, `source_identity`, `canonical_timestamp`, `normalize_domain`,
`normalize_source_url`。

## 四条硬约束如何落实

1. **逐字引文**：`literal_quote in snapshot_text`，不做大小写/空白归一化。归一化只用于
   claim key 与 statement 指纹。空白引文视为无证据。
2. **来源身份去重**：`source_id = sha256(url + "\n" + content_hash)`；快照按内容哈希寻址，
   重复灌入返回 `created=False`，候选 ID 由 `source_id + domain + claim_key + statement +
   literal_quote` 决定，跨进程重开仍稳定。URL 不同即不同来源。
3. **不能自动批准**：`ingest` 只允许 `pending`；approve 必须带具名 reviewer，拒绝自动化
   身份（`auto/system/machine/...`）与候选作者本人；审批时重新读取快照、重算哈希、重新
   逐字定位引文、检查同 subject 冲突；任一不满足抛 `ReviewBlocked`，状态最多升到
   `conflicted`，绝不进入 `approved`。
4. **候选不是事实**：模块不 import 记忆引擎、不打开事实库、无 `write_fact` 入口；
   `is_confirmed_fact` 恒 `False`；入库只能由后续阶段消费 `as_fact_proposal()` 显式完成。

## 验证

```text
$ cd agentcore
$ TMPDIR=/dev/shm python3 -m pytest tests/test_learning_candidates.py -q
40 passed in 0.23s

$ TMPDIR=/dev/shm python3 -m pytest tests/test_import_boundary.py \
    tests/test_business_neutrality.py tests/test_repo_neutrality.py -q
30 passed in 0.81s

$ TMPDIR=/dev/shm python3 -m pytest tests -q
446 passed, 9 skipped in 22.11s
```

9 个 skip 是既有的可选原生绑定缺失（`test_scheduler.py` / `test_worker.py`），与本切片无关。

`bash verify.sh`（静态 + 单测 + 安装 dry-run）：见本轮回执；未执行 docker 干净室。

## 剩余 T9 工作（本阶段明确未做）

1. **检索**：受预算控制的检索任务、抓取器、快照落盘（当前 ingest 只接收调用方已抓到的文本）。
2. **审查交互**：审查者如何取证、冲突如何裁决、`needs_human` 的人工入口与回执。
3. **正式记忆写入**：调用 `agentcore.memory.memory_store.write_fact()` 消费
   `as_fact_proposal()`，用稳定 source/candidate identity 防重复灌入；本阶段无任何写库路径。
4. **召回探针**：写入后 `flush_background()`，新会话真实召回核对命中与 scope 隔离
   （候选库不在默认召回路径上，尚未端到端实测）。
5. **冲突检测**：当前只对显式 `subject` 生效（同 subject 不同 statement）；无语义相似度
   推断，未声明 subject 的相反说法不会互相阻挡。
