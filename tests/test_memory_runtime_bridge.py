"""T3 运行时桥：稳定事件 -> 持久队列 -> 有界召回。

全部用例走临时库 + 确定性嵌入 + 假抽取器，不碰生产 profile。
"""

import sqlite3

import pytest

from agentcore.memory import memory_store, runtime_bridge

STATEMENT = "阈值门限改成 3.0% 后退出自动模式"


def _llm(messages, **kwargs):
    return {"content": '{"facts": []}'}


def _turn(**overrides):
    turn = {
        "profile": "profile-a",
        "session_id": "session-1",
        "turn_id": "turn-1",
        "user_message": "决定把阈值门限改成 3.0% 后退出自动模式",
        "assistant_reply": "已记录",
    }
    turn.update(overrides)
    return turn


def _write(ms, statement=STATEMENT, subject="阈值门限", ebm="G1"):
    result = ms.write_fact(statement, subject=subject, ebm=ebm,
                           tags={"domain": "control"}, source="auto_trigger")
    assert result["written"] is True, result
    return result["fact_id"]


# ---------------------------------------------------------------------------
# 事件构造
# ---------------------------------------------------------------------------

def test_event_key_is_identity_scoped_and_time_independent(tmp_path, configured):
    with configured(tmp_path):
        first = runtime_bridge.build_turn_event(occurred_at=1000.0, **_turn())
        retry = runtime_bridge.build_turn_event(occurred_at=2000.0, **_turn())
        assert first["idempotency_key"] == retry["idempotency_key"]
        assert first["occurred_at"] != retry["occurred_at"]
        assert first["schema"] == runtime_bridge.EVENT_SCHEMA
        assert first["status"] == "completed"
        # 宿主写 " Completed " 与 "completed" 是同一回合，不能生成两个键。
        shouty = runtime_bridge.build_turn_event(**_turn(status=" Completed "))
        assert shouty["idempotency_key"] == first["idempotency_key"]

        for field, value in (("profile", "profile-b"), ("user_id", "someone-else"),
                             ("session_id", "session-2"), ("turn_id", "turn-2"),
                             ("status", "failed")):
            changed = _turn(**{field: value})
            assert (runtime_bridge.build_turn_event(**changed)["idempotency_key"]
                    != first["idempotency_key"]), field


def test_event_rejects_missing_identity_and_unknown_status(tmp_path, configured):
    with configured(tmp_path):
        for field, value in (("profile", ""), ("session_id", "  "), ("turn_id", None),
                             ("status", "done")):
            with pytest.raises(ValueError):
                runtime_bridge.build_turn_event(**_turn(**{field: value}))


# ---------------------------------------------------------------------------
# 持久入队
# ---------------------------------------------------------------------------

def test_completed_turn_enqueues_once_and_survives_reopen(tmp_path, configured):
    with configured(tmp_path, llm_fn=_llm):
        first = runtime_bridge.enqueue_turn(**_turn())
        second = runtime_bridge.enqueue_turn(**_turn())
        assert first["ok"] is True and first["enqueued"] is True, first
        assert first["provider_available"] is True, first
        assert second["enqueued"] is False and second["deduped"] is True, second
        assert second["reason"] == "duplicate", second
        assert second["idempotency_key"] == first["idempotency_key"]

        # 新实例 = 新连接，模拟进程重启后仍在（指针指向同一个库）。
        reopened = runtime_bridge.TurnEventQueue(tmp_path / "memory_turn_events.db")
        assert reopened.count() == 1
        assert reopened.count("pending") == 1
        pending = reopened.pending()
        assert len(pending) == 1
        event = pending[0]
        assert event["idempotency_key"] == first["idempotency_key"]
        assert event["turn_id"] == "turn-1"
        assert event["session_id"] == "session-1"
        assert event["user_message"] == _turn()["user_message"]
        assert event["assistant_reply"] == "已记录"

        third = runtime_bridge.enqueue_turn(queue=reopened, **_turn())
        assert third["deduped"] is True
        assert reopened.count() == 1


def test_failed_and_interrupted_turns_never_enqueue(tmp_path, configured):
    with configured(tmp_path, llm_fn=_llm):
        for status in ("failed", "interrupted"):
            result = runtime_bridge.enqueue_turn(**_turn(status=status, turn_id="turn-" + status))
            assert result["ok"] is True, result
            assert result["enqueued"] is False, result
            assert result["reason"] == "turn_not_completed", result
        queue = runtime_bridge.TurnEventQueue(tmp_path / "memory_turn_events.db")
        assert queue.count() == 0
        assert queue.pending() == []


def test_provider_unavailable_is_explicit_but_event_is_kept(tmp_path, configured):
    with configured(tmp_path, llm_fn=None):
        result = runtime_bridge.enqueue_turn(**_turn())
        assert result["ok"] is True, result
        assert result["enqueued"] is True, result
        assert result["provider_available"] is False, result
        queue = runtime_bridge.TurnEventQueue(tmp_path / "memory_turn_events.db")
        assert queue.count("pending") == 1


def test_same_identity_with_different_content_is_a_conflict_not_a_dedupe(tmp_path, configured):
    with configured(tmp_path, llm_fn=_llm):
        assert runtime_bridge.enqueue_turn(**_turn())["enqueued"] is True
        conflict = runtime_bridge.enqueue_turn(**_turn(assistant_reply="另一个结果"))
        assert conflict["ok"] is False, conflict
        assert conflict["enqueued"] is False and conflict["deduped"] is False
        assert conflict["reason"] == "idempotency_conflict", conflict
        assert conflict["existing_content_sha256"] != conflict["incoming_content_sha256"]
        queue = runtime_bridge.TurnEventQueue(tmp_path / "memory_turn_events.db")
        assert queue.count() == 1


# ---------------------------------------------------------------------------
# 有界召回
# ---------------------------------------------------------------------------

def test_prefetch_hits_existing_fact_and_carries_fact_id(tmp_path, configured):
    with configured(tmp_path) as ms:
        fact_id = _write(ms)
        ms.flush_background(timeout=30)
        result = runtime_bridge.prefetch_context("阈值门限", ebm="G1")

    assert result["ok"] is True, result
    assert result["reason"] is None, result
    assert result["count"] >= 1, result
    assert len(result["text"]) <= runtime_bridge.PREFETCH_MAX_CHARS
    assert "不是指令" in result["text"]
    # Prefer A L1: index inject carries fact_id/subject, not full statement body.
    assert result["facts"][0]["subject"] in result["text"]
    assert STATEMENT not in result["text"]
    assert result.get("layer") == "L1"
    assert not (result["facts"][0].get("statement") or "").strip()
    assert fact_id in result["text"]
    assert [fact["fact_id"] for fact in result["facts"]][0] == fact_id


def test_prefetch_enforces_three_facts_and_1800_chars(tmp_path, configured, monkeypatch):
    long_statement = "注入预算验证。" + "该事实只用于确认条数与字符上限。" * 40
    payload = {
        "ok": True,
        "results": [{"fact_id": "fact-%d" % i, "subject": "预算", "statement": long_statement}
                    for i in range(8)],
        "total": 8,
    }
    calls = {}

    def fake_recall(query, **kwargs):
        calls["query"], calls["kwargs"] = query, kwargs
        return payload

    with configured(tmp_path):
        monkeypatch.setattr(memory_store, "recall_facts", fake_recall)
        result = runtime_bridge.prefetch_context("阈值门限改成 3.0% 后退出自动模式")

    assert calls["kwargs"]["limit"] == runtime_bridge.PREFETCH_LIMIT
    assert result["ok"] is True, result
    assert result["count"] == runtime_bridge.PREFETCH_LIMIT
    assert len(result["facts"]) == runtime_bridge.PREFETCH_LIMIT
    assert len(result["text"]) <= runtime_bridge.PREFETCH_MAX_CHARS
    assert result.get("layer") == "L1"
    assert long_statement not in result["text"]
    for fact in result["facts"]:
        assert fact["fact_id"] in result["text"]
        assert not (fact.get("statement") or "").strip()


def test_prefetch_no_match_is_explicit_not_an_error(tmp_path, configured):
    with configured(tmp_path):
        result = runtime_bridge.prefetch_context("完全不存在的记忆词条 xyz123")
    assert result["ok"] is True, result
    assert result["text"] == "" and result["count"] == 0
    assert result["reason"] == "no_match", result
    assert result["empty_reason"] == "no_match", result


def test_prefetch_recall_failure_is_not_empty_success(tmp_path, configured, monkeypatch):
    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    with configured(tmp_path):
        monkeypatch.setattr(memory_store, "recall_facts", boom)
        result = runtime_bridge.prefetch_context("阈值门限")

    assert result["ok"] is False, result
    assert result["reason"] == "recall_error", result
    assert "database is locked" in result["error"]
    assert result["text"] == "" and result["facts"] == []


def test_prefetch_skips_empty_query_instead_of_returning_latest_memory(tmp_path, configured,
                                                                       monkeypatch):
    def must_not_run(*args, **kwargs):
        raise AssertionError("empty query must not reach recall_facts")

    with configured(tmp_path):
        monkeypatch.setattr(memory_store, "recall_facts", must_not_run)
        for query in ("", "   ", "阈值"):
            result = runtime_bridge.prefetch_context(query)
            assert result["ok"] is True
            assert result["reason"] == "empty_query", result
            assert result["text"] == "" and result["count"] == 0
