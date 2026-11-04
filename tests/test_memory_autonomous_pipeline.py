"""P1 gold-marker tests for the isolated autonomous memory pipeline."""
from __future__ import annotations

import hashlib
import json
import logging
import time

import pytest

from agentcore.memory import memory_autonomy, memory_store, runtime_bridge
from agentcore.memory.event_worker import MemoryEventWorker
from agentcore.memory.runtime_service import scoped_runner
from agentcore.memory.turn_spool import TurnSpool


def _event(turn_id="turn-a", **overrides):
    fields = dict(profile="profile-a", user_id="user-a", agent_id="agent-a",
                  session_id="session-a", turn_id=turn_id,
                  user_message="以后回复我时保持简洁。",
                  assistant_reply="好的。", status="completed")
    fields.update(overrides)
    return runtime_bridge.build_turn_event(**fields)


def _queue(tmp_path):
    return runtime_bridge.TurnEventQueue(tmp_path / "events.db")


def _enqueue(queue, event):
    result = queue.enqueue(event)
    assert result["enqueued"] is True, result
    return event


def _model(statement, **extra):
    def call(messages, **options):
        assert set(options) == {"timeout_seconds"}
        fact = {"statement": statement, "user_quote": statement,
                "subject": "model-controlled-subject", "ebm": "G99",
                "tags": {"confidence": "high", "credential": "secret"}}
        fact.update(extra)
        return json.dumps({"facts": [fact]}, ensure_ascii=False)
    return call


def _pipeline(queue, statement, **kwargs):
    return memory_autonomy.AutonomousMemoryPipeline(
        queue=queue, model=_model(statement), **kwargs)


def _rows(queue):
    with queue._connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM turn_events ORDER BY created_at")]


def _facts():
    conn = memory_store._connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT f.*, (SELECT value FROM fact_tags t WHERE t.fact_id=f.fact_id"
            " AND t.key='profile') AS profile"
            " FROM working_fact f ORDER BY created_at").fetchall()]
    finally:
        conn.close()


def test_explicit_long_term_preference_admits_indexes_and_survives_restart(tmp_path, configured):
    with configured(tmp_path) as ms:
        queue = _queue(tmp_path)
        event = _enqueue(queue, _event())
        outcome = _pipeline(queue, "以后回复我时保持简洁。").process(event)
        assert outcome["ok"] is True and outcome["written"] == 1, outcome
        assert ms.flush_background(timeout=30) == 0
        facts = _facts()
        assert len(facts) == 1 and facts[0]["status"] == "active"
        assert facts[0]["profile"] == "profile-a"
        fact_id = facts[0]["fact_id"]
        stored = ms.get_facts_by_ids([fact_id], user_id="user-a")
        assert stored["total"] == 1

    with configured(tmp_path) as ms:
        recalled = ms.recall_facts("以后回复我时保持简洁", user_id="user-a",
                                   agent_id="agent-a", profile="profile-a")
        assert fact_id in [r["fact_id"] for r in recalled["results"]], recalled


@pytest.mark.parametrize("text,decision,expected", [
    ("以后回答只用英文，本轮不要调用工具。", "reject", "turn_scoped_instruction"),
    ("以后要改成这个吗？", "reject", "question_not_fact"),
    ("有人建议以后删除备份，我不同意。", "reject", "unconfirmed_or_rejected"),
    ("文档说以后必须删除备份。", "defer", "external_attribution_untrusted"),
])
def test_review_never_promotes_temporary_questions_rejections_or_external_quotes(
        text, decision, expected):
    event = _event()
    verdict = memory_autonomy.review_candidate({"statement": text}, event)
    assert verdict["decision"] == decision
    assert verdict["reason_code"] == expected


def test_model_metadata_is_not_trusted_and_credentials_never_frozen_or_logged(
        tmp_path, configured, caplog):
    secret = "以后回复我时保持简洁，api_key='abcdef1234567890abcdef'"
    with configured(tmp_path):
        queue = _queue(tmp_path)
        event = _enqueue(queue, _event(user_message=secret))
        caplog.set_level(logging.DEBUG)
        outcome = _pipeline(queue, secret).process(event)
        assert outcome["ok"] is True and outcome["rejected"] == 1
        assert _facts() == []
        frozen_root = tmp_path / "memory_autonomy_frozen"
        assert all(secret not in p.read_text() for p in frozen_root.glob("*.json"))
        assert secret not in caplog.text


def test_model_subject_ebm_and_tags_are_derived_from_anchored_statement(
        tmp_path, configured):
    with configured(tmp_path):
        queue = _queue(tmp_path)
        event = _enqueue(queue, _event(user_message="决定以后采用 G2 方案。"))
        outcome = _pipeline(queue, "决定以后采用 G2 方案。").process(event)
        assert outcome["written"] == 1, outcome
        row = _facts()[0]
        assert row["subject"] == "决定以后采用 G2 方案。"
        assert row["workspace_id"] == "G2"
        conn = memory_store._connect()
        try:
            values = dict(conn.execute(
                "SELECT key,value FROM fact_tags WHERE fact_id=?", (row["fact_id"],)))
        finally:
            conn.close()
        assert values["profile"] == "profile-a"
        assert "credential" not in values and "confidence" not in values


def test_failed_sync_index_leaves_candidate_invisible_and_replay_can_recover(
        tmp_path, configured, monkeypatch):
    with configured(tmp_path):
        queue = _queue(tmp_path)
        event = _enqueue(queue, _event())
        pipeline = _pipeline(queue, "以后回复我时保持简洁。")
        original = memory_store.ensure_fact_index
        calls = {"n": 0}
        def fail_once(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"ok": False, "reason": "vector_index_incomplete", "retryable": True}
            return original(*args, **kwargs)
        monkeypatch.setattr(memory_store, "ensure_fact_index", fail_once)
        first = pipeline.process(event)
        assert first["ok"] is False and first["retryable"] is True
        facts = _facts()
        assert len(facts) == 1 and facts[0]["status"] == "candidate"
        recalled = memory_store.recall_facts("以后回复我时保持简洁", user_id="user-a",
                                             agent_id="agent-a", profile="profile-a")
        assert recalled["results"] == []
        second = pipeline.process(event)
        assert second["ok"] is True and second["written"] == 1, second
        assert _facts()[0]["status"] == "active"
        assert pipeline.model_calls == 1


def test_duplicate_replay_rechecks_owner_status_index_and_recall(tmp_path, configured,
                                                                 monkeypatch):
    with configured(tmp_path):
        queue = _queue(tmp_path)
        event = _enqueue(queue, _event())
        pipeline = _pipeline(queue, "以后回复我时保持简洁。")
        assert pipeline.process(event)["ok"] is True
        original = memory_store.ensure_fact_index
        monkeypatch.setattr(memory_store, "ensure_fact_index",
                            lambda *a, **kw: {"ok": False, "reason": "index_unavailable"})
        replay = pipeline.process(event)
        assert replay["ok"] is False and replay["reason"] == "autonomous_admission_incomplete"
        assert any(r["reason"] == "duplicate_verification_failed"
                   for r in replay["results"])
        assert len(_facts()) == 1


def test_same_user_different_agent_and_profile_do_not_share_recall(tmp_path, configured):
    with configured(tmp_path):
        queue_a = _queue(tmp_path / "a")
        queue_b = _queue(tmp_path / "b")
        a = _enqueue(queue_a, _event())
        b = _enqueue(queue_b, _event(turn_id="turn-b", agent_id="agent-b",
                                     profile="profile-b"))
        assert _pipeline(queue_a, "以后回复我时保持简洁。").process(a)["written"] == 1
        assert _pipeline(queue_b, "以后回复我时保持简洁。").process(b)["written"] == 1
        own = memory_store.recall_facts("以后回复我时保持简洁", user_id="user-a",
                                        agent_id="agent-a", profile="profile-a")
        foreign_agent = memory_store.recall_facts(
            "以后回复我时保持简洁", user_id="user-a", agent_id="agent-b",
            profile="profile-a")
        foreign_profile = memory_store.recall_facts(
            "以后回复我时保持简洁", user_id="user-a", agent_id="agent-a",
            profile="profile-b")
        assert len(own["results"]) == 1
        assert foreign_agent["results"] == [] and foreign_profile["results"] == []


def test_low_jaccard_same_topic_reversal_reaches_judge(tmp_path, configured, monkeypatch):
    with configured(tmp_path) as ms:
        old = ms.write_fact("以后回答保持简洁。", subject="回复偏好",
                            tags={"profile": "profile-a"}, user_id="user-a",
                            agent_id="agent-a", source="manual")
        assert old["written"] is True, old
        # Isolate conflict judgment from asynchronous manual seed indexing.
        # Complete setup before requiring the candidate to reach the reviewer.
        assert ms.flush_background(timeout=30) == 0
        from agentcore.memory import memory_conflict
        calls = []
        def verdict(new, candidates, *, conservative=False, **kwargs):
            assert conservative is True
            calls.append(candidates)
            return {"action": "skip", "relation": "defer",
                    "reason": "synthetic_defer", "superseded": [], "judged": []}
        monkeypatch.setattr(memory_conflict, "resolve", verdict)
        queue = _queue(tmp_path)
        event = _enqueue(queue, _event(user_message="今后回答尽可能详细。"))
        outcome = _pipeline(queue, "今后回答尽可能详细。").process(event)
        assert outcome["ok"] is False and outcome["written"] == 0, outcome
        assert calls and old["fact_id"] in {row["fact_id"] for row in calls[0]}, outcome
        assert any(row["reason"] == "synthetic_defer"
                   for row in outcome["results"]), outcome


def test_autonomy_conflict_scan_is_bounded_and_defer_on_budget(tmp_path, configured,
                                                              monkeypatch):
    with configured(tmp_path) as ms:
        monkeypatch.setattr(memory_store, "AUTONOMY_CONFLICT_SCAN_BUDGET", 1)
        for index in range(2):
            ms.write_fact("决定以后采用第%d个门限。" % index, subject="门限%d" % index,
                          ebm="G1", tags={"profile": "profile-a"},
                          user_id="user-a", agent_id="agent-a", source="manual")
        queue = _queue(tmp_path)
        event = _enqueue(queue, _event(user_message="决定以后采用 G1 新门限。"))
        outcome = _pipeline(queue, "决定以后采用 G1 新门限。").process(event)
        assert outcome["ok"] is False and outcome["written"] == 0
        assert any(row["reason"] == "conflict_budget_exceeded"
                   for row in outcome["results"])
        assert all(row["status"] == "active" for row in _facts())


def test_conflict_judge_failure_keeps_old_fact_and_candidate_isolated(
        tmp_path, configured, monkeypatch):
    with configured(tmp_path) as ms:
        old = ms.write_fact("决定以后采用 3% 门限。", subject="门限", ebm="G1",
                            tags={"profile": "profile-a"}, user_id="user-a",
                            agent_id="agent-a", source="manual")
        assert old["written"] is True
        queue = _queue(tmp_path)
        event = _enqueue(queue, _event(user_message="决定以后采用 5% 门限。"))
        def failed_chat(*args, **kwargs):
            raise TimeoutError("private details")
        monkeypatch.setattr(memory_store.get_config(), "llm_fn",
                            failed_chat, raising=False)
        from agentcore.memory import memory_conflict
        monkeypatch.setattr(memory_conflict, "resolve", lambda new, candidates: {
            "action": "skip", "relation": "defer", "reason": "judge_failed",
            "superseded": [], "judged": []})
        outcome = _pipeline(queue, "决定以后采用 5% 门限。").process(event)
        assert outcome["ok"] is False and outcome["retryable"] is True
        assert outcome["written"] == 0
        facts = _facts()
        assert any(f["fact_id"] == old["fact_id"] and f["status"] == "active" for f in facts)
        assert any(f["status"] == "candidate" and "5%" in f["statement"] for f in facts)


def test_receipt_requires_event_statement_and_success_binding():
    event = _event()
    statement = "实测门限改为 5% 已通过。"
    digest = hashlib.sha256(statement.encode()).hexdigest()
    assert memory_autonomy.review_candidate(
        {"statement": statement}, event,
        source_receipt={"success": True, "exit_code": 1,
                        "event_key": event["idempotency_key"],
                        "statement_sha256": digest, "task_id": "t"})["decision"] == "defer"
    assert memory_autonomy.review_candidate(
        {"statement": statement}, event,
        source_receipt={"success": True, "exit_code": 0,
                        "event_key": "wrong", "statement_sha256": digest,
                        "task_id": "t"})["decision"] == "defer"
    assert memory_autonomy.review_candidate(
        {"statement": statement}, event,
        source_receipt={"success": True, "exit_code": 0,
                        "event_key": event["idempotency_key"],
                        "statement_sha256": digest, "task_id": "t"})["decision"] == "admit"


def test_default_chat_contract_and_injected_candidate_adapter_are_distinct(
        tmp_path, configured):
    calls = []
    def chat(messages, **options):
        calls.append(options)
        return {"content": '{"facts":[]}'}
    with configured(tmp_path, llm_fn=chat):
        queue = _queue(tmp_path)
        event = _enqueue(queue, _event())
        pipeline = memory_autonomy.AutonomousMemoryPipeline(queue=queue)
        result = pipeline.process(event)
        assert result["ok"] is True and result["skipped"] is True
        assert calls == [{"timeout": 45.0, "max_tokens": 800, "temperature": 0.0}]
    def candidate(messages, timeout_seconds):
        return '{"facts":[]}'
    with configured(tmp_path):
        queue = _queue(tmp_path / "adapter")
        event = _enqueue(queue, _event())
        result = memory_autonomy.AutonomousMemoryPipeline(
            queue=queue, model=candidate).process(event)
        assert result["ok"] is True


def test_automated_and_incomplete_events_do_not_call_model(tmp_path, configured):
    with configured(tmp_path):
        calls = []
        queue = _queue(tmp_path)
        pipeline = memory_autonomy.AutonomousMemoryPipeline(
            queue=queue, model=lambda *a, **kw: calls.append(1))
        assert pipeline.process(_event(status="failed"))["written"] == 0
        assert pipeline.process(_event(turn_id="s:cron:job"))["reason"] == "automated_turn"
        assert calls == []


def test_worker_marks_done_only_after_success_and_retries_recoverably(tmp_path, configured):
    with configured(tmp_path):
        queue = _queue(tmp_path)
        event = _enqueue(queue, _event())
        pipeline = _pipeline(queue, "以后回复我时保持简洁。")
        original = memory_store.ensure_fact_index
        calls = {"n": 0}
        def fail_once(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"ok": False, "reason": "vector_index_incomplete", "retryable": True}
            return original(*args, **kwargs)
        memory_store.ensure_fact_index = fail_once
        try:
            worker = MemoryEventWorker(queue=queue, event_runner=pipeline.process,
                                       owner="worker", lease_seconds=30,
                                       max_attempts=3, backoff_base=0)
            first = worker.process_one()
            assert first["state"] == "pending" and first["retryable"] is True
            second = worker.process_one()
            assert second["state"] == "done" and second["result"]["written"] == 1
            assert queue.count("done") == 1
        finally:
            memory_store.ensure_fact_index = original


def test_spool_quarantines_bad_records_and_still_enqueues_good_turn(tmp_path, configured):
    with configured(tmp_path):
        root = tmp_path / "spool"
        spool = TurnSpool(root, profile="profile-a", user_id="user-a", agent_id="agent-a")
        spool.capture(session_id="good", turn_id="turn-good",
                      user_message="以后回复我时保持简洁。", assistant_reply="好的")
        spool.finalize(session_id="good", turn_id="turn-good", completed=True)
        (root / "broken.json").write_text("{not-json")
        foreign = dict(spool.identity, user_id="other-user",
                       session_id="foreign", turn_id="turn-foreign",
                       user_message="bad", assistant_reply="bad", state="completed")
        (root / "foreign.json").write_text(json.dumps(foreign))
        queue = _queue(tmp_path)
        result = spool.ingest(lambda **kw: runtime_bridge.enqueue_turn(queue=queue, **kw))
        assert result["enqueued"] == 1 and result["quarantined"] == 2
        assert queue.count("pending") == 1


def test_audit_preserves_initial_decision_and_updates_final_result(tmp_path, configured):
    with configured(tmp_path):
        queue = _queue(tmp_path)
        event = _event()
        audit = memory_autonomy.AutonomyAudit(queue)
        candidate = {"statement": "决定以后采用方案甲。"}
        audit.record(event_key=event["idempotency_key"],
                     event_content_sha256=event["content_sha256"], event=event,
                     candidate=candidate, decision="defer", reason_code="judge_failed")
        audit.record(event_key=event["idempotency_key"],
                     event_content_sha256=event["content_sha256"], event=event,
                     candidate=candidate, decision="admit", reason_code="explicit_user_decision",
                     fact_id="fact-1")
        rows = audit.rows()
        assert len(rows) == 1
        assert rows[0]["decision"] == "defer"
        assert rows[0]["reason_code"] == "judge_failed"
        assert rows[0]["final_decision"] == "admit"
        assert rows[0]["final_reason_code"] == "explicit_user_decision"
        assert rows[0]["attempts"] == 2


def test_cross_generation_isolation_for_strict_identity(tmp_path, configured):
    with configured(tmp_path):
        q1 = _queue(tmp_path / "g1")
        q2 = _queue(tmp_path / "g2")
        e1 = _enqueue(q1, _event(turn_id="g1",
                                 user_message="决定以后 G1 门限采用 3%。"))
        e2 = _enqueue(q2, _event(turn_id="g2",
                                 user_message="决定以后 G2 门限采用 5%。"))
        assert _pipeline(q1, "决定以后 G1 门限采用 3%。").process(e1)["written"] == 1
        assert _pipeline(q2, "决定以后 G2 门限采用 5%。").process(e2)["written"] == 1
        g1 = memory_store.recall_facts("门限", user_id="user-a",
                                       agent_id="agent-a",
                                       profile="profile-a", ebm="G1")
        g2 = memory_store.recall_facts("门限", user_id="user-a",
                                       agent_id="agent-a",
                                       profile="profile-a", ebm="G2")
        assert [row["workspace_id"] for row in g1["results"]] == ["G1"]
        assert [row["workspace_id"] for row in g2["results"]] == ["G2"]


def test_short_technical_alias_query_uses_exact_identity_path(tmp_path, configured):
    with configured(tmp_path):
        queue = _queue(tmp_path)
        event = _enqueue(queue, _event(user_message="决定以后 G1 门限采用 3%。"))
        assert _pipeline(queue, "决定以后 G1 门限采用 3%。").process(event)["written"] == 1
        own = memory_store.recall_facts(
            "门限", user_id="user-a", agent_id="agent-a",
            profile="profile-a", ebm="G1")
        foreign = memory_store.recall_facts(
            "门限", user_id="user-a", agent_id="agent-b",
            profile="profile-a", ebm="G1")
        assert [row["workspace_id"] for row in own["results"]] == ["G1"]
        assert foreign["results"] == []


@pytest.mark.parametrize("message,quote", [
    ("我还没决定以后使用英文回复。", "以后使用英文回复"),
    ("以后可能采用 3% 门限，但还没确定。", "采用 3% 门限"),
])
def test_truncated_quote_keeps_complete_sentence_guards(
        tmp_path, configured, message, quote):
    with configured(tmp_path):
        queue = _queue(tmp_path)
        event = _enqueue(queue, _event(user_message=message))
        def model(messages, timeout_seconds):
            return json.dumps({"facts": [{"statement": quote,
                                          "user_quote": quote}]},
                              ensure_ascii=False)
        result = memory_autonomy.AutonomousMemoryPipeline(
            queue=queue, model=model).process(event)
        assert result["written"] == 0
        assert result["ok"] is True
        assert _facts() == []


def test_recall_verification_failure_rolls_back_activation_and_supersede(
        tmp_path, configured, monkeypatch):
    with configured(tmp_path) as ms:
        old = ms.write_fact("决定采用 3% 门限。", subject="门限", ebm="G1",
                            tags={"profile": "profile-a"}, user_id="user-a",
                            agent_id="agent-a", source="manual")
        assert old["written"] is True
        queue = _queue(tmp_path)
        event = _enqueue(queue, _event(user_message="决定采用 5% 门限。"))
        from agentcore.memory import memory_conflict
        monkeypatch.setattr(memory_conflict, "resolve",
                            lambda new, candidates, **kwargs: {
            "action": "write", "relation": "supersede", "reason": "test_supersede",
            "superseded": [old["fact_id"]], "judged": []})
        pipeline = _pipeline(queue, "决定采用 5% 门限。")
        monkeypatch.setattr(pipeline, "_verify_recall", lambda *a, **kw: False)
        # Force activation's in-transaction recall to fail by attacking only
        # the query path, not the transaction itself.
        original = memory_store.recall_facts
        def failed_recall(*args, **kwargs):
            if kwargs.get("_connection") is not None:
                return {"ok": False, "results": [], "reason": "recall_error"}
            return original(*args, **kwargs)
        monkeypatch.setattr(memory_store, "recall_facts", failed_recall)
        outcome = pipeline.process(event)
        assert outcome["ok"] is False and outcome["retryable"] is True
        facts = _facts()
        old_row = next(row for row in facts if row["fact_id"] == old["fact_id"])
        assert old_row["status"] == "active"
        assert any(row["status"] == "candidate" for row in facts)
        recalled = memory_store.recall_facts(
            "决定采用 5% 门限。", user_id="user-a", agent_id="agent-a",
            profile="profile-a", ebm="G1")
        assert old["fact_id"] in [row["fact_id"] for row in recalled["results"]]


def test_frozen_review_binds_event_content_and_identity(tmp_path, configured):
    with configured(tmp_path):
        store = memory_autonomy.FrozenReviewStore(_queue(tmp_path))
        review = {"proposed_ok": True, "proposed_reason": "review_required",
                  "candidates": [{"statement": "以后保持简洁。"}]}
        store.freeze("same-key", review, event_content_sha256="content-a",
                     identity_sha256="identity-a")
        assert store.load("same-key", event_content_sha256="content-a",
                          identity_sha256="identity-a")["candidates"] == review["candidates"]
        with pytest.raises(ValueError, match="frozen_review_event_mismatch"):
            store.load("same-key", event_content_sha256="content-b",
                       identity_sha256="identity-a")
        with pytest.raises(ValueError, match="frozen_review_identity_mismatch"):
            store.load("same-key", event_content_sha256="content-a",
                       identity_sha256="identity-b")


def test_runtime_prefetch_and_l1_l2_l3_enforce_identity(tmp_path, configured):
    with configured(tmp_path):
        queue = _queue(tmp_path)
        event = _enqueue(queue, _event(user_message="决定以后 G1 门限采用 3%。"))
        result = _pipeline(queue, "决定以后 G1 门限采用 3%。").process(event)
        assert result["written"] == 1
        fact_id = result["results"][0]["fact_id"]
        own = runtime_bridge.prefetch_context(
            "决定以后 G1 门限采用 3%。", ebm="G1", user_id="user-a",
            agent_id="agent-a", profile="profile-a")
        foreign = runtime_bridge.prefetch_context(
            "决定以后 G1 门限采用 3%。", ebm="G1", user_id="user-a",
            agent_id="agent-b", profile="profile-a")
        assert fact_id in [hit["fact_id"] for hit in own["facts"]]
        assert foreign["count"] == 0
        from agentcore.memory import progressive
        assert progressive.search(
            "门限", ebm="G1", user_id="user-a", agent_id="agent-a",
            profile="profile-a")["hits"]
        assert not progressive.search(
            "门限", ebm="G1", user_id="user-a", agent_id="agent-b",
            profile="profile-a")["hits"]
        assert progressive.timeline(
            [fact_id], user_id="user-a", agent_id="agent-a",
            profile="profile-a")["count"] == 1
        assert progressive.timeline(
            [fact_id], user_id="user-a", agent_id="agent-b",
            profile="profile-a")["count"] == 0
        assert progressive.get_observations(
            [fact_id], user_id="user-a", agent_id="agent-a",
            profile="profile-a")["count"] == 1
        assert progressive.get_observations(
            [fact_id], user_id="user-a", agent_id="agent-b",
            profile="profile-a")["count"] == 0


def test_conflict_supersede_never_crosses_profile_boundary(tmp_path, configured, monkeypatch):
    with configured(tmp_path) as ms:
        old = ms.write_fact("决定采用 3% 门限。", subject="门限", ebm="G1",
                            tags={"profile": "profile-b"}, user_id="user-a",
                            agent_id="agent-a", source="manual")
        assert old["written"] is True
        queue = _queue(tmp_path)
        event = _enqueue(queue, _event(user_message="决定采用 5% 门限。"))
        from agentcore.memory import memory_conflict
        monkeypatch.setattr(memory_conflict, "resolve",
                            lambda new, candidates, **kwargs: {
            "action": "write", "relation": "supersede",
            "reason": "test_supersede", "superseded": [old["fact_id"]],
            "judged": []})
        outcome = _pipeline(queue, "决定采用 5% 门限。").process(event)
        assert outcome["written"] == 1
        conn = memory_store._connect()
        try:
            status = conn.execute(
                "SELECT status FROM working_fact WHERE fact_id=?", (old["fact_id"],)
            ).fetchone()["status"]
        finally:
            conn.close()
        assert status == "active"


def test_invalid_json_retry_is_not_marked_success(tmp_path, configured):
    with configured(tmp_path):
        queue = _queue(tmp_path)
        event = _enqueue(queue, _event())
        calls = []
        def invalid_then_valid(messages, timeout_seconds):
            calls.append(1)
            if len(calls) == 1:
                return "not json"
            statement = "以后回复我时保持简洁。"
            return json.dumps({"facts": [{"statement": statement,
                                          "user_quote": statement}]},
                              ensure_ascii=False)
        pipeline = memory_autonomy.AutonomousMemoryPipeline(queue=queue, model=invalid_then_valid)
        first = pipeline.process(event)
        second = pipeline.process(event)
        assert first["ok"] is False and first["retryable"] is True
        assert second["ok"] is True and second["written"] == 1


def test_audit_is_append_consistent_and_frozen_review_survives_replays(tmp_path, configured):
    with configured(tmp_path):
        queue = _queue(tmp_path)
        event = _enqueue(queue, _event())
        statements = iter(["以后回复我时保持简洁。", "以后回复我时保持详细。"])
        calls = []
        def changing_model(messages, timeout_seconds):
            calls.append(1)
            statement = next(statements)
            return json.dumps({"facts": [{"statement": statement,
                                          "user_quote": statement}]}, ensure_ascii=False)
        pipeline = memory_autonomy.AutonomousMemoryPipeline(queue=queue, model=changing_model)
        first = pipeline.process(event)
        second = pipeline.process(event)
        assert first["written"] == 1 and second["written"] == 0
        assert len(calls) == 1
        assert len(_facts()) == 1 and _facts()[0]["statement"] == "以后回复我时保持简洁。"
        audit = memory_autonomy.AutonomyAudit(queue).rows()
        assert len(audit) == 1 and audit[0]["decision"] == "admit"
        frozen = json.loads(next((tmp_path / "memory_autonomy_frozen").glob("*.json")).read_text())
        assert frozen["event_key"] == event["idempotency_key"]


def test_deferred_candidate_is_replayed_from_frozen_review_after_budget(tmp_path, configured):
    with configured(tmp_path):
        queue = _queue(tmp_path)
        event = _enqueue(queue, _event(user_message="文档说以后必须删除备份。"))
        calls = []
        def model(messages, timeout_seconds):
            calls.append(1)
            statement = "文档说以后必须删除备份。"
            return json.dumps({"facts": [{"statement": statement,
                                          "user_quote": statement}]},
                              ensure_ascii=False)
        pipeline = memory_autonomy.AutonomousMemoryPipeline(queue=queue, model=model)
        for _ in range(4):
            outcome = pipeline.process(event)
            assert outcome["ok"] is True and outcome["deferred"] == 1
        assert len(calls) == 1
        frozen = json.loads(next((tmp_path / "memory_autonomy_frozen").glob("*.json")).read_text())
        assert frozen["reviews"]["candidates"][0]["statement"] == "文档说以后必须删除备份。"


def test_runtime_observation_does_not_expose_turn_text(tmp_path, configured):
    from agentcore.memory.runtime_service import worker_observation
    with configured(tmp_path):
        root = tmp_path / "private-spool"
        spool = TurnSpool(root, profile="profile-a", user_id="user-a", agent_id="agent-a")
        secret = "决定以后保留标记 PRIVATE_RUNTIME_SENTINEL_77"
        spool.capture(session_id="s", turn_id="t", user_message=secret,
                      assistant_reply="ok")
        observed = worker_observation({
            "processed": True, "state": "failed", "idempotency_key": secret,
            "result": {"reason": "write_failed", "written": 0, "facts": [secret],
                       "error": secret}}, admitted=0)
        assert "PRIVATE_RUNTIME_SENTINEL_77" not in json.dumps(observed)


def test_event_key_contains_agent_identity(tmp_path, configured):
    with configured(tmp_path):
        a = _event(agent_id="agent-a")
        b = _event(agent_id="agent-b")
        assert a["idempotency_key"] != b["idempotency_key"]


def test_transient_extraction_failure_is_retried_not_frozen(tmp_path, configured):
    with configured(tmp_path):
        queue = _queue(tmp_path)
        event = _enqueue(queue, _event())
        calls = []
        def flaky(messages, timeout_seconds):
            calls.append(1)
            if len(calls) == 1:
                raise TimeoutError("provider detail")
            statement = "以后回复我时保持简洁。"
            return json.dumps({"facts": [{"statement": statement,
                                          "user_quote": statement}]},
                              ensure_ascii=False)
        pipeline = memory_autonomy.AutonomousMemoryPipeline(queue=queue, model=flaky)
        first = pipeline.process(event)
        second = pipeline.process(event)
        assert first["ok"] is False and first["retryable"] is True
        assert second["ok"] is True and second["written"] == 1
        assert len(calls) == 2


def test_explicit_decision_with_claimed_execution_still_needs_receipt():
    event = _event()
    verdict = memory_autonomy.review_candidate(
        {"statement": "决定以后采用实测 5% 门限。"}, event)
    assert verdict["decision"] == "defer"
    assert verdict["reason_code"] == "technical_evidence_missing"


def test_runtime_scoped_runner_uses_autonomous_pipeline(tmp_path, configured):
    with configured(tmp_path):
        queue = _queue(tmp_path)
        event = _enqueue(queue, _event())
        runner = scoped_runner(profile="profile-a", user_id="user-a",
                               agent_id="agent-a", queue=queue,
                               model=_model("以后回复我时保持简洁。"))
        result = runner(event)
        assert result["ok"] is True and result["written"] == 1


def test_failed_candidate_does_not_block_later_event(tmp_path, configured, monkeypatch):
    with configured(tmp_path):
        queue = _queue(tmp_path)
        first = _enqueue(queue, _event(turn_id="bad-index"))
        second = _enqueue(queue, _event(turn_id="good-index",
                                        user_message="以后所有回答都说明验证边界。"))
        bad_id = None
        def fail_bad(fact_id, **kwargs):
            nonlocal bad_id
            if bad_id is None:
                bad_id = fact_id
                return {"ok": False, "reason": "vector_index_incomplete"}
            return original(fact_id, **kwargs)
        original = memory_store.ensure_fact_index
        monkeypatch.setattr(memory_store, "ensure_fact_index", fail_bad)
        worker = MemoryEventWorker(
            queue=queue,
            event_runner=memory_autonomy.AutonomousMemoryPipeline(
                queue=queue, model=lambda messages, timeout_seconds:
                    json.dumps({"facts": [{"statement": messages[-1]["content"].split(
                        "【用户】\n", 1)[1].split("\n\n【助手】", 1)[0],
                        "user_quote": messages[-1]["content"].split(
                            "【用户】\n", 1)[1].split("\n\n【助手】", 1)[0]}]},
                        ensure_ascii=False)).process,
            owner="worker", max_attempts=1, backoff_base=0)
        first_out = worker.process_one()
        second_out = worker.process_one()
        assert first_out["state"] == "failed"
        assert second_out["state"] == "done"
        statuses = sorted(row["status"] for row in _facts())
        assert statuses == ["active", "candidate"]


def test_offline_pipeline_completes_without_manual_review(tmp_path, configured):
    with configured(tmp_path):
        queue = _queue(tmp_path)
        event = _enqueue(queue, _event())
        result = _pipeline(queue, "以后回复我时保持简洁。").process(event)
        assert result["ok"] is True and result["written"] == 1 and result["rejected"] == 0
