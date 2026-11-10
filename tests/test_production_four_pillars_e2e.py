"""Non-mock end-to-end verification of the four production pillars (Phases 0-4)."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import sys
from pathlib import Path

import pytest


def test_phase0_deinflation_and_doc_links():
    """Phase 0: Verify false external claims are removed and README doc links exist."""
    repo_root = Path(__file__).resolve().parents[1]
    targets = [
        repo_root / "README.md",
        repo_root / "docs" / "ARCHITECTURE.md",
        repo_root / "docs" / "TECH_MANUAL.md",
        repo_root / "agentcore" / "preflight.py",
        repo_root / "agentcore" / "safety_envelope.py",
        repo_root / "agentcore" / "colleague_pack.py",
        repo_root / "agentcore" / "pool.py",
        repo_root / "agentcore" / "memory" / "memory_surprise.py",
        repo_root / "plugins" / "durable_dispatch" / "__init__.py",
    ]
    forbidden_terms = ("KunAutoDrive", "OpenMausBot", "OpenHands-style")
    for path in targets:
        content = path.read_text(encoding="utf-8")
        for term in forbidden_terms:
            assert term not in content, f"Forbidden term {term!r} still present in {path}"

    surprise_text = (repo_root / "agentcore" / "memory" / "memory_surprise.py").read_text(encoding="utf-8")
    assert "Heuristic Surprise Gate" in surprise_text


def test_phase1_fts5_bm25_and_vec0_rrf_hybrid_recall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Phase 1: Verify real SQLite FTS5 + bm25() + vec0 RRF (k=60) hybrid recall and query plan."""
    from agentcore.memory import config as mem_cfg
    from agentcore.memory import memory_store
    from agentcore.memory.hybrid_recall import recall_hybrid, rrf_fuse
    from agentcore.memory.memory_scale import explain_fts_query_plan, fts_bm25_search, index_stats

    monkeypatch.setenv("MEMORY_PERSIST_DIR", str(tmp_path))
    mem_cfg.configure(mem_cfg.MemoryConfig(data_dir=tmp_path, embed_fn=lambda texts: None))
    db_path = mem_cfg.get_config().db_path()

    res1 = memory_store.write_fact(
        "自动驾驶SIL多容器并发DDS共享内存零拷贝与无锁环形队列规约",
        subject="sil_dds_zero_copy",
        tags={"kind": "rule", "scope": "principle"},
    )
    res2 = memory_store.write_fact(
        "交互卡片仅承载状态结论展示，长文本统一写入交付物文件发送",
        subject="interactive_card_rule",
        tags={"kind": "rule", "scope": "ops"},
    )
    assert res1["ok"] is True
    assert res2["ok"] is True

    conn = sqlite3.connect(str(db_path))
    fts_count = conn.execute("SELECT COUNT(*) FROM fact_fts").fetchone()[0]
    assert fts_count >= 2

    plan = explain_fts_query_plan(conn, "自动驾驶 零拷贝")
    assert any("fact_fts" in detail and "VIRTUAL TABLE INDEX" in detail for detail in plan), plan

    hits = fts_bm25_search(conn, "零拷贝 环形队列", limit=5)
    conn.close()
    assert hits, "Expected FTS5 BM25 search to return hits"
    assert hits[0]["bm25_rank"] == 1

    st = index_stats(str(db_path))
    assert st["ok"] is True
    assert st["fts_ready"] is True
    assert st["fts_rows"] >= 2
    assert st["strategy"] in ("fts5_bm25_only", "fts5_bm25+vec0_rrf")

    hybrid = recall_hybrid(
        "DDS共享内存零拷贝",
        limit=5,
        entity_db=tmp_path / "entities.db",
        rrf_k=60,
    )
    assert hybrid["ok"] is True
    assert hybrid["rrf_k"] == 60
    assert hybrid["strategy"] == st["strategy"]
    assert any("零拷贝" in r.get("statement", "") for r in hybrid["results"])

    fused = rrf_fuse(
        [{"statement": "规则A", "subject": "a"}],
        [{"statement": "规则A", "subject": "a"}, {"statement": "规则B", "subject": "b"}],
        rrf_k=60,
    )
    assert fused[0]["statement"] == "规则A"
    assert abs(fused[0]["_rrf_score"] - round(2.0 / 61.0, 6)) < 1e-5


def test_phase2_skill_forge_auto_distillation_and_prompt_mounting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Phase 2: Verify automatic case->markdown skill distillation and prompt mounting into input.json."""
    from agentcore.skill_forge import AgentCase, SkillForge
    from install.worker import dev_exec_runner

    forge_dir = tmp_path / "skill-forge"
    attempt_dir = tmp_path / "attempt-1"
    attempt_dir.mkdir(parents=True)
    monkeypatch.setenv("SUPERAGENT_SKILL_FORGE_DIR", str(forge_dir))
    monkeypatch.setenv("SUPERAGENT_ATTEMPT_DIR", str(attempt_dir))
    monkeypatch.setenv("DEV_MEMORY_INJECT", "0")

    forge = SkillForge(storage_dir=forge_dir)
    case = AgentCase(
        case_id="",
        task_intent="修复 SQLite FTS5 中文分词检索与 vec0 RRF 混合召回链路",
        approach="启用 fact_fts 虚表同步并执行 bm25 排序与 RRF 融合",
        quality_score=95.0,
        key_insight="Ensure fact_fts is populated via tokenize_for_fts on every commit_turn",
        domain="autodrive",
        role="cases",
        verified_files=["agentcore/memory/memory_scale.py", "agentcore/memory/memory_store.py"],
        approved=True,
    )
    forge.record_case(case)

    distilled = forge.auto_distill_from_cases(min_cases=1, min_quality=80.0)
    assert len(distilled) >= 1
    skill = distilled[0]
    md_files = list((forge_dir / "skills").rglob("*.md"))
    assert len(md_files) >= 1
    assert md_files[0].stat().st_size > 100

    composed = dev_exec_runner._compose_goal(
        "请核验 SQLite FTS5 中文分词检索与 RRF 混合召回实现",
        {"role": "cases"},
    )
    assert "Verified Procedural Skills" in composed
    assert skill.name in composed

    input_json_path = attempt_dir / "input.json"
    assert input_json_path.is_file()
    input_data = json.loads(input_json_path.read_text(encoding="utf-8"))
    assert skill.skill_id in input_data.get("mounted_skills", [])
    assert "Verified Procedural Skills" in input_data.get("composed_goal", "")


def test_phase3_state_graph_sqlite_checkpointer_and_resume(tmp_path: Path):
    """Phase 3: Verify StateGraph + SqliteCheckpointer node persistence and interruption resume in dev_runner."""
    from agentcore.dev_runner import run_dev_job
    from agentcore.state_graph import Checkpoint, SqliteCheckpointer

    workdir = tmp_path / "workdir"
    workdir.mkdir(parents=True)
    (workdir / "src").mkdir()
    (workdir / "src" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")

    out_dir = tmp_path / "attempt_out"
    out_dir.mkdir(parents=True)
    agent_dir = tmp_path / "agent_dir"

    ckpt_db = out_dir / "state_checkpoints.db"
    ckpt = SqliteCheckpointer(ckpt_db)
    # Simulate an interrupted prior attempt that completed node 1 (prepare_context)
    ckpt.put(
        Checkpoint(
            task_id="job-resume-e2e",
            step=1,
            node="prepare_context",
            state={
                "goal": "Update src/mod.py",
                "effective_goal": "Update src/mod.py",
                "selected_model": "flash",
                "degraded": False,
                "degradation_reason": "",
                "routing_role": "cases",
            },
        )
    )

    def _runner_adapter(prompt_str, ctx):
        p = Path(ctx["workdir"]) / "src" / "mod.py"
        p.write_text("VALUE = 2\n", encoding="utf-8")
        return {
            "ok": True,
            "exit_code": 0,
            "final_text": "Updated src/mod.py inside resumed StateGraph",
            "tool_calls": [{"name": "write", "path": "src/mod.py"}],
            "duration_ms": 25,
            "model": ctx.get("model", "flash"),
        }

    res = run_dev_job(
        {
            "job": "job-resume-e2e",
            "attempt": "1",
            "resume": True,
            "payload": {"goal": "Update src/mod.py", "role": "cases"},
        },
        policy={"allowed_roots": [str(workdir)]},
        role={"system_prompt": "cases"},
        workdir=workdir,
        agent_dir=agent_dir,
        output_dir=out_dir,
        sandbox=False,
        runner_callable=_runner_adapter,
    )
    assert res["ok"] is True
    assert ckpt_db.is_file() and ckpt_db.stat().st_size > 0

    history = ckpt.list("job-resume-e2e")
    nodes = [entry.node for entry in history]
    assert nodes == ["prepare_context", "execute_model", "verify_outputs", "harvest_artifacts"]

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["resumed_from_node"] == "prepare_context"
    assert manifest["checkpoint_db"] == str(ckpt_db)


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap not installed on host")
def test_phase4_pool_supervisor_bwrap_sandbox_production_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Phase 4: Verify Pool -> attempt_supervisor -> dev_exec_runner -> AttemptBoundSandboxAdapter (bwrap) chain."""
    from agentcore.colleague_containment import AttemptBoundSandboxAdapter
    from agentcore.pool import Pool

    if not AttemptBoundSandboxAdapter.is_bwrap_functional():
        pytest.skip("bwrap user/mount namespaces not permitted in this container/kernel environment")

    pool_root = tmp_path / "pool"
    workdir = tmp_path / "workspace"
    workdir.mkdir(parents=True)
    forge_dir = tmp_path / "skill-forge"

    fake_hermes = workdir / "fake_hermes_probe.py"
    fake_hermes.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "etc_blocked = False\n"
        "try:\n"
        "    pathlib.Path('/etc/superagent_bwrap_forbidden_probe').write_text('x')\n"
        "except OSError:\n"
        "    etc_blocked = True\n"
        "ssh_dir = pathlib.Path.home() / '.ssh'\n"
        "ssh_masked = True\n"
        "if ssh_dir.exists():\n"
        "    try:\n"
        "        (ssh_dir / 'bwrap_probe').write_text('x')\n"
        "        ssh_masked = False\n"
        "    except OSError:\n"
        "        ssh_masked = True\n"
        "att_dir = pathlib.Path(os.environ['SUPERAGENT_ATTEMPT_DIR'])\n"
        "deliverable = att_dir / 'sandbox_verification_report.md'\n"
        "deliverable.write_text('# Sandbox Verification\\nRead-only root and credential masking verified.\\n', encoding='utf-8')\n"
        "print(json.dumps({\n"
        "    'sandbox_backend': os.environ.get('SUPERAGENT_SANDBOX_BACKEND'),\n"
        "    'etc_write_blocked': etc_blocked,\n"
        "    'ssh_write_blocked': ssh_masked,\n"
        "    'deliverable': str(deliverable),\n"
        "}))\n",
        encoding="utf-8",
    )
    fake_hermes.chmod(fake_hermes.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    monkeypatch.setenv("DEV_HERMES_BIN", str(fake_hermes))
    monkeypatch.setenv("DEV_WORKDIR", str(workdir))
    monkeypatch.setenv("DEV_MEMORY_INJECT", "0")
    monkeypatch.setenv("SUPERAGENT_SKILL_FORGE_DIR", str(forge_dir))

    runner_script = Path(__file__).resolve().parents[1] / "install" / "worker" / "dev_exec_runner.py"
    p = Pool(pool_root)
    job_id = p.submit(
        "dedupe-phase4-bwrap-e2e",
        {
            "goal": "Execute sandbox containment verification probe and emit deliverable report",
            "role": "cases",
        },
    )

    ran = p.run_once([sys.executable, str(runner_script)], timeout=30.0)
    assert ran is True

    job_state = p.status(job_id)
    assert job_state["status"] == "completed", job_state
    res_obj = json.loads(job_state["result"]) if isinstance(job_state["result"], str) else job_state["result"]
    assert res_obj["ok"] is True

    attempt_id = job_state["attempt"]
    attempt_dir = pool_root / "attempts" / attempt_id
    binding_file = attempt_dir / "binding.json"
    containment_audit_file = attempt_dir / "containment_audit.json"
    ckpt_db_file = attempt_dir / "state_checkpoints.db"
    input_file = attempt_dir / "input.json"

    assert binding_file.is_file()
    assert containment_audit_file.is_file()
    assert ckpt_db_file.is_file() and ckpt_db_file.stat().st_size > 0
    assert input_file.is_file()

    audit_data = json.loads(containment_audit_file.read_text(encoding="utf-8"))
    assert audit_data["backend"] == "bwrap"
    assert audit_data["binding"]["ok"] is True
    assert "--unshare-user" in audit_data["bwrap_argv"]
    assert "--ro-bind" in audit_data["bwrap_argv"]
    assert "--tmpfs" in audit_data["bwrap_argv"]

    inner_res = res_obj.get("result") or res_obj
    probe_out = json.loads(inner_res["summary"])
    assert probe_out["sandbox_backend"] == "bwrap"
    assert probe_out["etc_write_blocked"] is True
    assert probe_out["ssh_write_blocked"] is True
    assert any(a.endswith("sandbox_verification_report.md") for a in inner_res.get("artifacts", []))
