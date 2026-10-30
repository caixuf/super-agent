"""T3 isolated auto-memory loop: decision -> write -> reopen -> recall."""

import json

from agentcore.memory import memory_trigger

FACT = {
    "subject": "阈值门限",
    "statement": "阈值门限改成 3.0% 后退出自动模式",
    "ebm": "G1",
    "kind": "technical",
    "tags": {"domain": "control"},
}


def _llm(messages, **kwargs):
    return {"content": json.dumps({"facts": [FACT]}, ensure_ascii=False)}


def test_decision_writes_once_and_survives_reopen(tmp_path, configured):
    with configured(tmp_path, llm_fn=_llm) as ms:
        result = memory_trigger.run(
            "决定把阈值门限改成 3.0% 后退出自动模式", "已记录", source="auto_trigger")
        assert result["ok"] is True and result["written"] == 1, result
        assert ms.flush_background(timeout=30) == 0
        first = ms.recall_facts("阈值门限", limit=3, ebm="G1")
        assert FACT["statement"] in [row["statement"] for row in first["results"]], first

    # 新配置/新连接模拟重启后的跨会话召回。
    with configured(tmp_path) as ms:
        again = ms.recall_facts("阈值门限", limit=3, ebm="G1")
        assert FACT["statement"] in [row["statement"] for row in again["results"]], again


def test_same_decision_twice_does_not_duplicate(tmp_path, configured):
    with configured(tmp_path, llm_fn=_llm) as ms:
        first = memory_trigger.run("决定把阈值门限改成 3.0% 后退出自动模式", "已记录")
        second = memory_trigger.run("决定把阈值门限改成 3.0% 后退出自动模式", "已记录")
        assert first["written"] == 1, first
        assert second["written"] == 0, second
        assert second["results"][0].get("reason") == "duplicate", second
        assert ms.flush_background(timeout=30) == 0
        recalled = ms.recall_facts("阈值门限", limit=5, ebm="G1")
        matches = [row for row in recalled["results"] if row["statement"] == FACT["statement"]]
        assert len(matches) == 1, recalled


def test_provider_unavailable_is_not_reported_as_empty_success(tmp_path, configured):
    with configured(tmp_path, llm_fn=None):
        result = memory_trigger.run("决定把阈值门限改成 3.0% 后退出自动模式", "已记录")
        assert result["ok"] is False, result
        assert result["reason"] == "provider_unavailable", result
        assert result["retryable"] is True
        assert result["written"] == 0


def test_incomplete_turn_never_becomes_a_confirmed_fact(tmp_path, configured):
    with configured(tmp_path, llm_fn=_llm) as ms:
        result = memory_trigger.run("决定把阈值门限改成 3.0% 后退出自动模式", "已记录",
                                    turn_status="failed")
        assert result["skipped"] is True and result["reason"] == "turn_not_completed", result
        assert result["written"] == 0
        assert ms.flush_background(timeout=30) == 0
        recalled = ms.recall_facts("阈值门限", limit=5, ebm="G1")
        assert FACT["statement"] not in [row["statement"] for row in recalled["results"]]
