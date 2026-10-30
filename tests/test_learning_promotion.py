"""候选晋升 + 召回探针的离线验收。

约束：候选库与正式记忆库都落在 ``tmp_path``；嵌入由 conftest 的确定性实现注入；
不联网、不写宿主生产记忆库、不依赖真实审查界面。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from agentcore.learning import (
    STATUS_APPROVED,
    STATUS_CONFLICTED,
    STATUS_PENDING,
    STATUS_REJECTED,
    CandidateError,
    CandidateStore,
    InvalidCandidate,
    ReviewBlocked,
    UnknownCandidate,
)
from agentcore.learning.promotion import (
    PROBE_ERROR,
    PROBE_HIT,
    PROBE_NO_MATCH,
    PROBE_SCOPE_LEAK,
    PROMOTION_SOURCE,
    CandidateNotPromotable,
    PromotionWriteFailed,
    promote_candidate,
    recall_probe,
    stable_fact_id,
)

SNAPSHOT = (
    "Widget 4.2 release notes\n"
    "Widget 4.2 was released on 2026-03-04.\n"
    "The widget firmware can be\n"
    "updated over the air.\n"
)
QUOTE = "Widget 4.2 was released on 2026-03-04."
URL = "https://example.test/widget-4.2/release-notes"
RETRIEVED = "2026-10-20T09:30:00Z"
SUBJECT = "widget-4.2-release-date"
REVIEWER = "reviewer-human"
PROMOTER = "promoter-human"

SNAPSHOT_APRIL = "Widget 4.2 shipped in April 2026.\nMaintenance window: Fridays.\n"
QUOTE_APRIL = "Widget 4.2 shipped in April 2026."


def make_store(tmp_path):
    return CandidateStore(tmp_path / "candidate-pool")


def ingest(store, **overrides):
    kwargs = dict(
        statement=QUOTE,
        literal_quote=QUOTE,
        source_url=URL,
        retrieved_at=RETRIEVED,
        domain="Reference",
        snapshot=SNAPSHOT,
        subject=SUBJECT,
        author="retriever-1",
    )
    kwargs.update(overrides)
    return store.ingest(**kwargs)


def approved_candidate(store, **overrides):
    candidate = ingest(store, **overrides).candidate
    return store.review(candidate.candidate_id, reviewer=REVIEWER, decision="approve")


class WithdrawnStore:
    """候选库契约里没有 withdrawn 状态；用同形状假库验证「非 approved 一律拒绝」。"""

    def __init__(self, candidate):
        self._candidate = candidate

    def get(self, candidate_id):
        if candidate_id != self._candidate.candidate_id:
            raise UnknownCandidate("未知候选：%s" % candidate_id)
        return self._candidate


class FailingMemory:
    def __init__(self, result=None, error=None):
        self.result = result or {}
        self.error = error

    def write_fact(self, statement, **kwargs):
        if self.error is not None:
            raise self.error
        return self.result


class BrokenRecall:
    def __init__(self, *, flush_error=None, recall_error=None):
        self.flush_error = flush_error
        self.recall_error = recall_error

    def flush_background(self, timeout=30.0):
        if self.flush_error is not None:
            raise self.flush_error
        return 0

    def recall_facts(self, query, **kwargs):
        if self.recall_error is not None:
            raise self.recall_error
        raise AssertionError("未预期的召回调用")


class LeakingRecall:
    """模拟跨用户隔离失效：任何 user_id 都返回同一条事实。"""

    def flush_background(self, timeout=30.0):
        return 0

    def recall_facts(self, query, *, user_id=None, ebm=None, tags=None, limit=5):
        return {"ok": True, "total": 1, "results": [{"fact_id": "fact-leaked"}]}


# ── 1. approved 晋升：一次性写入 + 幂等重试 ────────────────────────────────
def test_approved_candidate_promotes_once_then_dedupes(engine, tmp_path):
    store = make_store(tmp_path)
    candidate = approved_candidate(store)
    assert candidate.review_status == STATUS_APPROVED

    first = promote_candidate(store, candidate.candidate_id,
                              reviewer=PROMOTER, memory_store=engine)
    assert first.ok is True
    assert first.written is True and first.deduped is False
    assert first.fact_id == stable_fact_id(candidate.candidate_id)
    assert first.candidate_id == candidate.candidate_id
    assert first.statement == candidate.statement
    assert first.source == PROMOTION_SOURCE
    assert first.reviewer == PROMOTER
    assert first.provenance_match is True

    second = promote_candidate(store, candidate.candidate_id,
                               reviewer=PROMOTER, memory_store=engine)
    assert second.ok is True
    assert (second.written, second.deduped) == (False, True)
    assert second.fact_id == first.fact_id
    assert second.reason == "duplicate"
    assert second.provenance_match is True

    # 候选账本不改：状态仍是 approved，候选依旧不是事实。
    stored = store.get(candidate.candidate_id)
    assert stored == candidate
    assert stored.review_status == STATUS_APPROVED
    assert stored.is_confirmed_fact is False
    assert len(store.attempts(candidate.candidate_id)) == 1

    # 正式库只有一条，且溯源标签完整。
    assert engine.flush_background(timeout=30) == 0
    recalled = engine.recall_facts(candidate.statement,
                                   user_id=engine.get_config().default_user,
                                   tags={"candidate_id": candidate.candidate_id},
                                   limit=10)
    assert recalled["ok"] is True and len(recalled["results"]) == 1
    row = recalled["results"][0]
    assert row["fact_id"] == first.fact_id
    assert row["statement"] == candidate.statement
    assert row["source"] == PROMOTION_SOURCE
    assert row["tags"]["candidate_id"] == candidate.candidate_id
    assert row["tags"]["source_id"] == candidate.source_id
    assert row["tags"]["source_url"] == candidate.source_url
    assert row["tags"]["source_hash"] == candidate.source_hash
    assert row["tags"]["promotion_reviewer"] == PROMOTER


def test_promotion_does_not_append_to_candidate_ledgers(engine, tmp_path):
    store = make_store(tmp_path)
    candidate = approved_candidate(store)
    candidates_before = (store.root / "candidates.jsonl").read_text(encoding="utf-8")
    reviews_before = (store.root / "reviews.jsonl").read_text(encoding="utf-8")

    promote_candidate(store, candidate.candidate_id, reviewer=PROMOTER, memory_store=engine)

    assert (store.root / "candidates.jsonl").read_text(encoding="utf-8") == candidates_before
    assert (store.root / "reviews.jsonl").read_text(encoding="utf-8") == reviews_before


# ── 2. 非 approved 候选一律拒绝 ────────────────────────────────────────────
def test_pending_candidate_is_refused(engine, tmp_path):
    store = make_store(tmp_path)
    candidate = ingest(store).candidate
    assert candidate.review_status == STATUS_PENDING

    with pytest.raises(CandidateNotPromotable) as excinfo:
        promote_candidate(store, candidate.candidate_id, reviewer=PROMOTER, memory_store=engine)
    assert STATUS_PENDING in str(excinfo.value)

    engine.flush_background(timeout=30)
    assert engine.recall_facts("", user_id=engine.get_config().default_user)["results"] == []
    assert store.get(candidate.candidate_id) == candidate


def test_rejected_candidate_is_refused(engine, tmp_path):
    store = make_store(tmp_path)
    candidate = ingest(store).candidate
    rejected = store.review(candidate.candidate_id, reviewer=REVIEWER, decision="reject")
    assert rejected.review_status == STATUS_REJECTED

    with pytest.raises(CandidateNotPromotable):
        promote_candidate(store, candidate.candidate_id, reviewer=PROMOTER, memory_store=engine)
    assert store.get(candidate.candidate_id) == rejected


def test_conflicted_candidate_is_refused(engine, tmp_path):
    store = make_store(tmp_path)
    approved_candidate(store)

    conflicting = ingest(
        store,
        statement=QUOTE_APRIL,
        literal_quote=QUOTE_APRIL,
        snapshot=SNAPSHOT_APRIL,
        source_url="https://example.test/widget-4.2/april",
    ).candidate
    with pytest.raises(ReviewBlocked):
        store.review(conflicting.candidate_id, reviewer=REVIEWER, decision="approve")
    conflicted = store.get(conflicting.candidate_id)
    assert conflicted.review_status == STATUS_CONFLICTED

    with pytest.raises(CandidateNotPromotable):
        promote_candidate(store, conflicting.candidate_id, reviewer=PROMOTER, memory_store=engine)


def test_withdrawn_candidate_is_refused(engine, tmp_path):
    store = make_store(tmp_path)
    approved = approved_candidate(store)
    withdrawn = replace(approved, review_status="withdrawn")
    fake = WithdrawnStore(withdrawn)
    assert withdrawn.is_confirmed_fact is False

    with pytest.raises(CandidateNotPromotable):
        promote_candidate(fake, withdrawn.candidate_id, reviewer=PROMOTER, memory_store=engine)


def test_unknown_candidate_is_refused(engine, tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(UnknownCandidate):
        promote_candidate(store, "cand_does_not_exist", reviewer=PROMOTER, memory_store=engine)


def test_missing_reviewer_is_refused(engine, tmp_path):
    store = make_store(tmp_path)
    candidate = approved_candidate(store)
    for bad in ("", "   ", None):
        with pytest.raises(InvalidCandidate):
            promote_candidate(store, candidate.candidate_id, reviewer=bad, memory_store=engine)


# ── 3. 写入失败要显式上抛，不能伪装成成功 ──────────────────────────────────
def test_write_failure_is_surfaced(engine, tmp_path):
    store = make_store(tmp_path)
    candidate = approved_candidate(store)

    with pytest.raises(PromotionWriteFailed):
        promote_candidate(store, candidate.candidate_id, reviewer=PROMOTER,
                          memory_store=FailingMemory(result={"ok": False, "error": "db locked"}))
    with pytest.raises(PromotionWriteFailed):
        promote_candidate(store, candidate.candidate_id, reviewer=PROMOTER,
                          memory_store=FailingMemory(error=RuntimeError("disk full")))
    with pytest.raises(PromotionWriteFailed):
        promote_candidate(store, candidate.candidate_id, reviewer=PROMOTER,
                          memory_store=FailingMemory(result={"ok": True, "written": True}))
    # 失败的晋升不改候选状态
    assert store.get(candidate.candidate_id) == candidate


# ── 4. 召回探针 ────────────────────────────────────────────────────────────
def test_recall_probe_hits_after_asynchronous_write(engine, tmp_path):
    store = make_store(tmp_path)
    candidate = approved_candidate(store)
    promoted = promote_candidate(store, candidate.candidate_id,
                                 reviewer=PROMOTER, memory_store=engine)

    # 刻意不手动 flush：探针内部必须自己汇合后台向量写入。
    probe = recall_probe(promoted.fact_id, candidate.statement, memory_store=engine)
    assert probe.ok is True
    assert probe.status == PROBE_HIT
    assert probe.matched is True
    assert probe.fact_id in probe.fact_ids
    assert probe.scope_checked is True
    assert probe.scope_isolated is True
    assert probe.leaked_to is None


def test_recall_probe_reports_no_match(engine, tmp_path):
    store = make_store(tmp_path)
    candidate = approved_candidate(store)
    promoted = promote_candidate(store, candidate.candidate_id,
                                 reviewer=PROMOTER, memory_store=engine)

    probe = recall_probe(promoted.fact_id, "completely unrelated zebra quantum banana",
                         memory_store=engine)
    assert probe.ok is False
    assert probe.status == PROBE_NO_MATCH
    assert probe.matched is False
    assert probe.fact_id not in probe.fact_ids


def test_recall_probe_reports_flush_error(engine):
    probe = recall_probe("fact-1", "anything",
                         memory_store=BrokenRecall(flush_error=RuntimeError("flush boom")))
    assert probe.status == PROBE_ERROR
    assert probe.matched is False
    assert "flush boom" in probe.error


def test_recall_probe_reports_recall_error(engine):
    probe = recall_probe("fact-1", "anything",
                         memory_store=BrokenRecall(recall_error=RuntimeError("recall down")))
    assert probe.status == PROBE_ERROR
    assert "recall down" in probe.error


def test_recall_probe_detects_scope_leak():
    probe = recall_probe("fact-leaked", "anything", memory_store=LeakingRecall())
    assert probe.status == PROBE_SCOPE_LEAK
    assert probe.matched is True
    assert probe.scope_checked is True
    assert probe.scope_isolated is False
    assert probe.leaked_to is not None and probe.leaked_to != "__default_user__"
    assert probe.ok is False


def test_scope_check_can_be_disabled(engine, tmp_path):
    store = make_store(tmp_path)
    candidate = approved_candidate(store)
    promoted = promote_candidate(store, candidate.candidate_id,
                                 reviewer=PROMOTER, memory_store=engine)

    probe = recall_probe(promoted.fact_id, candidate.statement,
                         memory_store=engine, check_scope=False)
    assert probe.status == PROBE_HIT
    assert probe.scope_checked is False
    assert probe.scope_isolated is None
    assert probe.ok is True


def test_cross_user_isolation_on_real_store(engine, tmp_path):
    store = make_store(tmp_path)
    candidate = approved_candidate(store)
    promoted = promote_candidate(store, candidate.candidate_id,
                                 reviewer=PROMOTER, memory_store=engine)

    owner_probe = recall_probe(promoted.fact_id, candidate.statement, memory_store=engine)
    assert owner_probe.status == PROBE_HIT

    intruder_probe = recall_probe(promoted.fact_id, candidate.statement,
                                  memory_store=engine, user_id="intruder-user")
    assert intruder_probe.status == PROBE_NO_MATCH
    assert intruder_probe.matched is False
    assert intruder_probe.scope_isolated is True

    assert engine.recall_facts(candidate.statement, user_id="intruder-user",
                               limit=10)["results"] == []


# ── 5. 稳定身份 ────────────────────────────────────────────────────────────
def test_stable_fact_id_is_deterministic():
    first = stable_fact_id("cand_abc")
    assert first == stable_fact_id("cand_abc") == stable_fact_id("  cand_abc  ")
    assert first != stable_fact_id("cand_def")
    assert first.startswith("candfact_") and len(first) == len("candfact_") + 32
    with pytest.raises(CandidateError):
        stable_fact_id("")
