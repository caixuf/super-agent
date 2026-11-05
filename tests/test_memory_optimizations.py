# -*- coding: utf-8 -*-
"""Comprehensive verification suite for Memory System Optimizations (P0-P4)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Sequence

import pytest

from agentcore.memory import (
    MemoryConfig,
    apply_task_outcome,
    configure,
    drop_fact,
    flush_background,
    prune_dormant_vectors,
    recall_facts,
    recall_hybrid,
    reset_config,
    rrf_fuse,
    supersede,
    sync_databases,
    write_fact,
)
from agentcore.memory.entity_store import EntityStore
from agentcore.memory.read_view import MemoryReadView


def _deterministic_embed(texts: Sequence[str], dim: int = 16) -> list[list[float]]:
    out: list[list[float]] = []
    for text in texts:
        vec = [0.0] * dim
        for i, ch in enumerate(text):
            vec[i % dim] += float((ord(ch) % 17) + 1)
        norm = sum(x * x for x in vec) ** 0.5 or 1.0
        out.append([x / norm for x in vec])
    return out


def test_p0_entity_store_external_seed_pack_and_dynamic_anchors(tmp_path: Path) -> None:
    seed_pack = {
        "entities": [
            {
                "entity_id": "ent_custom_scheduler",
                "name": "quantum_scheduler",
                "entity_type": "subsystem",
                "aliases": ["qsched", "量子调度器"],
                "description": "Custom deterministic scheduler",
            }
        ],
        "rules": [
            {
                "rule_id": "rule_qsched_lease",
                "entity_id": "ent_custom_scheduler",
                "polarity": "prohibition",
                "condition_expr": "when claiming lease in quantum_scheduler",
                "action_stmt": "禁止在持有量子调度器锁时执行阻塞网络调用",
                "confidence": 1.0,
            }
        ],
    }
    seed_file = tmp_path / "custom_seed.json"
    seed_file.write_text(json.dumps(seed_pack, ensure_ascii=False), encoding="utf-8")

    db_path = tmp_path / "entities_custom.db"
    store = EntityStore(db_path=db_path, seed_file=seed_file)

    hits = store.recall("quantum_scheduler 锁阻塞网络调用怎么处理", limit=3)
    assert len(hits) >= 1
    assert hits[0]["fact_id"] == "rule_qsched_lease"
    assert "禁止在持有量子调度器锁时执行阻塞网络调用" in hits[0]["statement"]

    exported_path = tmp_path / "exported_seed.json"
    exported = store.export_seed_pack(exported_path)
    assert exported_path.is_file()
    assert any(e["entity_id"] == "ent_custom_scheduler" for e in exported["entities"])

    empty_db = tmp_path / "entities_empty.db"
    store2 = EntityStore(db_path=empty_db, auto_seed=False)
    assert store2.recall("quantum_scheduler", limit=3) == []
    stats = store2.import_seed_pack(exported_path)
    assert stats["entities_imported"] >= 1
    assert stats["rules_imported"] >= 1
    hits2 = store2.recall("qsched", limit=3)
    assert any(h["fact_id"] == "rule_qsched_lease" for h in hits2)


def test_p1_dormant_vector_gc_on_retire_and_read_view_eviction(tmp_path: Path) -> None:
    data_dir = tmp_path / "mem_p1"
    data_dir.mkdir(mode=0o700)
    configure(
        MemoryConfig(
            data_dir=data_dir,
            default_user="u1",
            default_agent="a1",
            embed_dim=16,
            embed_fn=lambda texts: _deterministic_embed(texts, dim=16),
        )
    )
    try:
        r1 = write_fact(
            "Old brake threshold is 3.0 percent",
            subject="threshold",
            user_id="u1",
            agent_id="a1",
            tags={"profile": "p1"},
        )
        r2 = write_fact(
            "Updated brake threshold is 5.0 percent",
            subject="threshold",
            user_id="u1",
            agent_id="a1",
            tags={"profile": "p1"},
        )
        r3 = write_fact(
            "Temporary diagnostic note to deprecate",
            subject="diag",
            user_id="u1",
            agent_id="a1",
            tags={"profile": "p1"},
        )
        assert flush_background(timeout=5.0) == 0
        f1, f2, f3 = r1["fact_id"], r2["fact_id"], r3["fact_id"]

        db_file = data_dir / "coding_memory_cv.db"
        conn = sqlite3.connect(str(db_file))
        try:
            emb_ids_before = {
                row[0] for row in conn.execute("SELECT fact_id FROM fact_embeddings").fetchall()
            }
            assert {f1, f2, f3}.issubset(emb_ids_before)
        finally:
            conn.close()

        # Supersede f1 -> f2 and drop f3: vectors for f1 and f3 must be immediately purged.
        assert supersede(f1, f2, reason="updated spec")["ok"] is True
        assert drop_fact(f3, reason="obsolete")["ok"] is True

        conn = sqlite3.connect(str(db_file))
        try:
            emb_ids_after = {
                row[0] for row in conn.execute("SELECT fact_id FROM fact_embeddings").fetchall()
            }
            assert f1 not in emb_ids_after
            assert f3 not in emb_ids_after
            assert f2 in emb_ids_after

            # Simulate a legacy dormant embedding left over from older versions
            conn.execute(
                "INSERT INTO fact_embeddings(fact_id, model, dim, vector, text_hash, updated_at)"
                " VALUES (?, 'bge-m3', 16, ?, 'deadbeef', '2026-01-01T00:00:00')",
                (f1, b"\x00" * 64),
            )
            conn.commit()
        finally:
            conn.close()

        # MemoryReadView with compact_dormant=True evicts dormant vectors in-memory
        from agentcore.memory import change_journal as journal, memory_store as store
        source = store._connect()
        journal.install(source)
        view = MemoryReadView(source, compact_dormant=True)
        try:
            in_mem_emb = {
                row[0]
                for row in view.conn.execute("SELECT fact_id FROM fact_embeddings").fetchall()
            }
            assert f1 not in in_mem_emb
            assert f2 in in_mem_emb
        finally:
            view.close()
            source.close()

        # prune_dormant_vectors cleans up the legacy dormant embedding on disk
        dry = prune_dormant_vectors(dry_run=True)
        assert dry["dormant_embeddings"] == 1
        pruned = prune_dormant_vectors(dry_run=False, vacuum=True)
        assert pruned["embeddings_deleted"] == 1
        assert prune_dormant_vectors(dry_run=True)["dormant_embeddings"] == 0
    finally:
        reset_config()


def test_p2_hybrid_recall_rrf_fusion_and_corroboration(tmp_path: Path) -> None:
    entity_hits = [
        {"rule_id": "r1", "statement": "Shared safety rule across both stores", "polarity": "prohibition"},
        {"rule_id": "r2", "statement": "Entity-only rule item", "polarity": "affirmative"},
    ]
    memory_hits = [
        {"fact_id": "f1", "statement": "Memory-only top fact"},
        {"fact_id": "f2", "statement": "Shared safety rule across both stores"},
    ]
    fused = rrf_fuse(entity_hits, memory_hits, limit=5, rrf_k=60)
    assert len(fused) == 3
    # The corroborated statement appears in both entity_hits and memory_hits so RRF ranks it #1
    assert fused[0]["statement"] == "Shared safety rule across both stores"
    assert fused[0]["sources"] == ["entity", "memory"]
    assert fused[0]["fact_id"] == "f2"
    assert fused[0]["rule_id"] == "r1"

    # End-to-end single-process recall_hybrid with trace recording
    data_dir = tmp_path / "mem_p2"
    data_dir.mkdir(mode=0o700)
    entity_db = tmp_path / "entity_p2.db"
    EntityStore(db_path=entity_db)
    configure(
        MemoryConfig(
            data_dir=data_dir,
            default_user="u2",
            default_agent="a2",
            embed_dim=16,
            embed_fn=lambda texts: _deterministic_embed(texts, dim=16),
        )
    )
    try:
        w = write_fact(
            "3090 server requires local bge-m3 embedding sidecar",
            subject="3090",
            user_id="u2",
            agent_id="a2",
            tags={"profile": "p2"},
        )
        assert flush_background(timeout=5.0) == 0
        res = recall_hybrid(
            "3090",
            limit=5,
            entity_db=entity_db,
            user_id="u2",
            agent_id="a2",
            profile="p2",
            task_id="task_hybrid_01",
            fusion="rrf",
        )
        assert res["ok"] is True
        assert res["entity_count"] >= 1
        assert res["memory_count"] >= 1
        assert any(r.get("fact_id") == w["fact_id"] for r in res["results"])
    finally:
        reset_config()


def test_p3_gate_plasticity_and_sync_end_to_end(tmp_path: Path) -> None:
    data_dir = tmp_path / "mem_p3"
    data_dir.mkdir(mode=0o700)
    configure(
        MemoryConfig(
            data_dir=data_dir,
            default_user="u3",
            default_agent="a3",
            embed_dim=16,
            embed_fn=lambda texts: _deterministic_embed(texts, dim=16),
        )
    )
    try:
        # 1. Gate enforcement rejects vague/non-actionable statement
        rejected = write_fact(
            "也许可能差不多吧",
            subject="vague",
            enforce_gate=True,
        )
        assert rejected["written"] is False
        assert rejected["reason"] == "rejected_by_gate"

        # 2. Gate enforcement admits concrete, scoped technical fact
        admitted = write_fact(
            "修改 schema.sql 后必须同步递增 SCHEMA_VERSION 常量值为 12",
            subject="schema_migration",
            ebm="G2",
            enforce_gate=True,
            user_id="u3",
            agent_id="a3",
            tags={"profile": "p3"},
        )
        assert admitted["written"] is True
        fid = admitted["fact_id"]
        assert flush_background(timeout=5.0) == 0

        # 3. Recall with task_id records usage trace -> apply_task_outcome updates synapses
        rec = recall_facts(
            "schema_migration",
            user_id="u3",
            agent_id="a3",
            profile="p3",
            task_id="task_pass_1",
        )
        assert rec["ok"] is True
        assert any(r["fact_id"] == fid for r in rec["results"])

        outcome_ok = apply_task_outcome("task_pass_1", passed=True)
        assert outcome_ok["ok"] is True
        assert outcome_ok["updated"] == 1
        assert outcome_ok["results"][0]["fact_id"] == fid
        assert outcome_ok["results"][0]["status"] == "active"

        # 5 consecutive non-environmental failures demote the memory to suspect
        for idx in range(5):
            tid = f"task_fail_{idx}"
            recall_facts("schema_migration", user_id="u3", agent_id="a3", profile="p3", task_id=tid)
            out_fail = apply_task_outcome(tid, passed=False, failure_text="AssertionError: version mismatch")
        assert out_fail["results"][0]["fact_id"] == fid
        assert out_fail["results"][0]["status"] == "suspect"

        # 4. Incremental database sync copies missing active facts + tags to a second DB
        dst_dir = tmp_path / "mem_p3_dst"
        dst_dir.mkdir(mode=0o700)
        dst_db = dst_dir / "coding_memory_cv.db"
        sync_res = sync_databases(str(data_dir / "coding_memory_cv.db"), str(dst_db))
        assert sync_res["ok"] is True
        assert sync_res["exported"] == 1
        assert sync_res["written"] == 1

        # Second sync is idempotent (0 exported)
        sync_again = sync_databases(str(data_dir / "coding_memory_cv.db"), str(dst_db))
        assert sync_again["ok"] is True
        assert sync_again["exported"] == 0
    finally:
        reset_config()
