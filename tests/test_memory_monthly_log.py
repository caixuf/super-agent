"""Prefer A — needle monthly log append + idempotency."""

from __future__ import annotations

from pathlib import Path

from agentcore.memory import memory_store, monthly_log
from agentcore.memory.memory_kind import OBS_DECISION, classify_observation_class, is_needle


def test_needle_write_appends_one_monthly_line(tmp_path, configured):
    with configured(tmp_path) as ms:
        r = ms.write_fact(
            "决定把频率门限改成 3.0% 后退出自动模式",
            subject="频率门限",
            ebm="G1",
            tags={"domain": "control", "observation_class": OBS_DECISION},
            source="test",
        )
        assert r["written"] is True, r
        path = monthly_log.monthly_log_path()
        assert path.is_file(), path
        text = path.read_text(encoding="utf-8")
        assert r["fact_id"] in text
        assert "decision" in text
        assert "频率门限" in text
        assert text.count("id=%s" % r["fact_id"]) == 1


def test_monthly_log_idempotent_by_fact_id(tmp_path, configured):
    with configured(tmp_path) as ms:
        r = ms.write_fact(
            "决定采用蓝绿发布",
            subject="发布",
            ebm="G1",
            tags={"observation_class": "decision"},
            source="test",
        )
        path = Path(monthly_log.monthly_log_path())
        before = path.read_text(encoding="utf-8")
        again = monthly_log.append_needle(
            fact_id=r["fact_id"],
            observation_class="decision",
            subject="发布",
            statement="决定采用蓝绿发布",
        )
        assert again["appended"] is False
        assert again["reason"] == "duplicate_fact_id"
        assert path.read_text(encoding="utf-8") == before


def test_non_needle_write_skips_monthly_log(tmp_path, configured):
    with configured(tmp_path) as ms:
        r = ms.write_fact(
            "Linux 内核调度使用 CFS",
            subject="调度",
            tags={"domain": "system", "kind": "context"},
            source="test",
        )
        assert r["written"] is True, r
        log_dir = tmp_path / "memory" / "log"
        assert not log_dir.exists() or not any(log_dir.glob("*.md"))


def test_classify_and_is_needle_helpers():
    assert is_needle("decision")
    assert is_needle("bugfix")
    assert not is_needle("note")
    assert classify_observation_class("根因是锁顺序反了导致死锁", "锁") == "bugfix"
    assert classify_observation_class("普通说明文字没有针类信号", "说明") is None


def test_memory_trigger_written_appends_needle(tmp_path, configured, monkeypatch):
    from agentcore.memory import memory_trigger

    def fake_extract(user_message, assistant_reply="", context="", strict=False,
                     require_user_evidence=False):
        return [{
            "subject": "门限",
            "statement": "决定把门限改成 2.5%",
            "ebm": "G1",
            "tags": {"observation_class": "decision", "kind": "technical"},
        }]

    with configured(tmp_path, llm_fn=lambda messages, **kw: {"content": "{}"}):
        monkeypatch.setattr(memory_trigger, "extract_facts", fake_extract)
        monkeypatch.setattr(memory_trigger, "should_consider",
                            lambda *a, **k: {"consider": True, "reason": "x", "matched": ["决定"]})
        out = memory_trigger.run("决定把门限改成 2.5%", "已记录")
        assert out.get("written", 0) >= 1, out
        path = monthly_log.monthly_log_path()
        assert path.is_file(), path
        assert "decision" in path.read_text(encoding="utf-8")
