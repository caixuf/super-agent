"""Prefer A — progressive disclosure L1/L2/L3 facade."""

from __future__ import annotations

import pytest

from agentcore.memory import memory_store, progressive


def _seed(ms, *, subject, statement, tags=None, ebm="G1"):
    r = ms.write_fact(
        statement,
        subject=subject,
        ebm=ebm,
        tags=tags or {"domain": "control"},
        source="test",
    )
    assert r.get("ok") and r.get("written"), r
    return r["fact_id"]


def test_search_l1_omits_statement_body_and_caps(tmp_path, configured):
    with configured(tmp_path) as ms:
        fid = _seed(ms, subject="频率门限", statement="决定把频率门限改成 3.0% 后退出自动模式")
        ms.flush_background(timeout=30)
        out = progressive.search("频率门限", ebm="G1", limit=5)
    assert out["ok"] is True, out
    assert out["layer"] == "L1"
    assert out["count"] >= 1
    hit = out["hits"][0]
    assert hit["fact_id"] == fid
    assert hit["subject"] == "频率门限"
    assert hit.get("statement") is None
    assert hit.get("body_included") is False
    assert "cost_hint" in hit
    # hard cap honest trim
    capped = progressive.search("频率门限", ebm="G1", limit=100, hard_cap=2)
    assert capped["trimmed"] is True
    assert capped["cap"] == 2
    assert capped["count"] <= 2


def test_timeline_stubs_are_chronological_and_body_light(tmp_path, configured):
    with configured(tmp_path) as ms:
        a = _seed(ms, subject="A", statement="决定采用方案甲作为默认路径")
        b = _seed(ms, subject="B", statement="决定采用方案乙替换甲")
        ms.flush_background(timeout=30)
        out = progressive.timeline([b, a])
    assert out["ok"] is True, out
    assert out["layer"] == "L2"
    assert out["count"] == 2
    assert all(h.get("statement") is None for h in out["hits"])
    assert all(h.get("body_included") is False for h in out["hits"])
    created = [h["created_at"] for h in out["hits"]]
    assert created == sorted(created)


def test_get_observations_full_only_for_requested_ids(tmp_path, configured):
    with configured(tmp_path) as ms:
        fid = _seed(ms, subject="门限", statement="决定把频率门限改成 3.0%")
        other = _seed(ms, subject="其它", statement="决定保留旧参数作为回退")
        ms.flush_background(timeout=30)
        out = progressive.get_observations([fid, "missing-id-xyz"])
    assert out["ok"] is True, out
    assert out["layer"] == "L3"
    assert out["count"] == 1
    assert out["missing_ids"] == ["missing-id-xyz"]
    row = out["results"][0]
    assert row["fact_id"] == fid
    assert "3.0%" in row["statement"]
    assert row["body_included"] is True
    assert "tags" in row
    # other id not requested → not returned
    assert other not in {r["fact_id"] for r in out["results"]}


def test_get_observations_cap_trim_or_raise(tmp_path, configured):
    with configured(tmp_path) as ms:
        ids = [
            _seed(ms, subject="S%d" % i, statement="决定写入第 %d 条针类事实用于封顶" % i)
            for i in range(5)
        ]
        ms.flush_background(timeout=30)
        trimmed = progressive.get_observations(ids, hard_cap=2)
        assert trimmed["trimmed"] is True
        assert trimmed["count"] == 2
        with pytest.raises(progressive.CapExceeded):
            progressive.get_observations(ids, hard_cap=2, raise_on_cap=True)


def test_search_stacks_on_recall_facts(tmp_path, configured, monkeypatch):
    calls = {}

    def fake_recall(query, **kwargs):
        calls["query"] = query
        calls["kwargs"] = kwargs
        return {
            "ok": True,
            "results": [{
                "fact_id": "f1",
                "subject": "subj",
                "statement": "FULL BODY MUST NOT LEAK AT L1",
                "details": "secret-details",
                "created_at": "2026-10-31T01:00:00",
                "tags": {"kind": "technical", "observation_class": "decision"},
            }],
        }

    with configured(tmp_path):
        monkeypatch.setattr(memory_store, "recall_facts", fake_recall)
        out = progressive.search("anything", limit=3)
    assert calls["query"] == "anything"
    assert out["hits"][0]["statement"] is None
    assert "FULL BODY" not in str(out)
