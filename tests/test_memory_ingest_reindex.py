from agentcore.memory import memory_ingest, memory_store


def test_ingest_reports_incomplete_reindex_after_facts_were_written(tmp_path, monkeypatch):
    doc = tmp_path / "sample.md"
    doc.write_text("# 运行规则\n\n新会话应当自动召回用户授权范围内的有效记忆。\n", encoding="utf-8")
    monkeypatch.setattr(memory_store, "write_fact", lambda *args, **kwargs: {"written": True})
    partial = {"ok": False, "error": "facts_changed_during_embedding", "updated": 1, "skipped_changed": 1}
    monkeypatch.setattr(memory_store, "reindex_embeddings", lambda: partial)

    result = memory_ingest.ingest_paths([str(doc)], dry_run=False)

    assert result["written"] > 0
    assert result["reindexed"] == 1
    assert result["ok"] is False
    assert result["error"] == "vector_reindex_incomplete"
    assert result["reindex_result"] == partial


def test_ingest_reports_reindex_exception_without_hiding_committed_facts(tmp_path, monkeypatch):
    doc = tmp_path / "sample.md"
    doc.write_text("# 运行规则\n\n新会话应当自动召回用户授权范围内的有效记忆。\n", encoding="utf-8")
    monkeypatch.setattr(memory_store, "write_fact", lambda *args, **kwargs: {"written": True})
    def fail():
        raise RuntimeError("unavailable")
    monkeypatch.setattr(memory_store, "reindex_embeddings", fail)

    result = memory_ingest.ingest_paths([str(doc)], dry_run=False)

    assert result["written"] > 0
    assert result["ok"] is False
    assert result["error"] == "vector_reindex_failed"
