"""Unit tests for EverOS-style SkillForge and Procedural Memory."""
from __future__ import annotations

from pathlib import Path
import pytest

from agentcore.skill_forge import AgentCase, ProceduralSkill, SkillForge


def test_agent_case_lifecycle():
    case = AgentCase(
        case_id="case_001",
        task_intent="Refactor durable queue transaction lock",
        approach="Use SQLite WAL transaction with busy_timeout and retry decorator",
        quality_score=95.0,
        key_insight="Avoid holding flock across network I/O calls",
        domain="core_storage",
        role="backend",
        verified_files=["agentcore/pool.py", "tests/test_pool.py"],
        approved=True,
    )
    d = case.to_dict()
    assert d["case_id"] == "case_001"
    assert d["quality_score"] == 95.0

    restored = AgentCase.from_dict(d)
    assert restored.case_id == case.case_id
    assert restored.approach == case.approach
    assert restored.verified_files == case.verified_files


def test_procedural_skill_markdown_serialization():
    skill = ProceduralSkill(
        skill_id="skill_safe_retry",
        name="Durable Retry Procedure",
        description="Standard pattern for transient error handling with exponential backoff",
        procedure_markdown="1. Catch transient exceptions (TimeoutError, ConnectionError)\n2. Sleep 2^attempt * base\n3. Assert idempotency token",
        domain="resilience",
        trigger_patterns=["timeout", "retry", "backoff"],
        confidence=0.9,
        maturity_score=0.75,
        source_case_ids=["case_001", "case_002"],
    )

    md = skill.to_markdown()
    assert "skill_id: skill_safe_retry" in md
    assert "maturity_score: 0.75" in md
    assert "Durable Retry Procedure" in md

    loaded = ProceduralSkill.from_markdown(md)
    assert loaded.skill_id == skill.skill_id
    assert loaded.name == skill.name
    assert loaded.description == skill.description
    assert loaded.domain == skill.domain
    assert loaded.maturity_score == 0.75
    assert "Catch transient exceptions" in loaded.procedure_markdown


def test_skill_forge_in_memory_distillation_and_recall():
    forge = SkillForge(in_memory=True)

    case1 = AgentCase(
        case_id="c1",
        task_intent="Fix async subreaper orphan leak on Linux",
        approach="Configure PR_SET_CHILD_SUBREAPER before fork and collect via SIGCHLD",
        quality_score=90.0,
        domain="runtime",
    )
    case2 = AgentCase(
        case_id="c2",
        task_intent="Ensure subreaper collects deep descendant processes",
        approach="Iterate /proc/self/task/pid/children and signal SIGKILL",
        quality_score=92.0,
        domain="runtime",
    )

    forge.record_case(case1)
    forge.record_case(case2)
    assert len(forge.list_cases()) == 2

    # Distill into a procedural skill
    skill = forge.distill_skill_from_cases(
        cases=[case1, case2],
        name="Linux Subreaper Process Isolation",
        description="Guaranteed child and grandchild cleanup using Linux kernel subreaper",
        procedure_markdown="1. Verify linux platform\n2. Call libc prctl PR_SET_CHILD_SUBREAPER\n3. Kill descendants on teardown",
        domain="runtime",
        trigger_patterns=["subreaper", "process", "leak", "orphan"],
    )

    assert skill.domain == "runtime"
    assert skill.confidence >= 0.9
    assert len(skill.source_case_ids) == 2

    # Recall matching skill
    recalled = forge.recall_skills("How to prevent subreaper child process leak", domain="runtime")
    assert len(recalled) >= 1
    assert recalled[0].skill_id == skill.skill_id

    # Promote maturity
    orig_maturity = skill.maturity_score
    promoted = forge.promote_skill(skill.skill_id, delta=0.1)
    assert promoted is not None
    assert promoted.maturity_score > orig_maturity

    # Render guidance string for prompt injection
    guidance = forge.render_guidance("prevent process leak", domain="runtime")
    assert "Linux Subreaper Process Isolation" in guidance
    assert "操作规程" in guidance


def test_skill_forge_disk_cascade_sync(tmp_path: Path):
    storage = tmp_path / "skill_storage"
    forge1 = SkillForge(storage_dir=storage)

    case = AgentCase(
        case_id="c_disk",
        task_intent="Add SQLite WAL checkpoint flush",
        approach="Execute PRAGMA wal_checkpoint(TRUNCATE)",
        quality_score=88.0,
    )
    forge1.record_case(case)

    skill = ProceduralSkill(
        skill_id="skill_sqlite_wal",
        name="SQLite WAL Checkpoint Sync",
        description="Flush and truncate WAL before releasing persistent lease",
        procedure_markdown="1. Execute PRAGMA wal_checkpoint(TRUNCATE)\n2. Close connection cleanly",
        domain="storage",
        trigger_patterns=["sqlite", "wal", "checkpoint"],
        maturity_score=0.8,
    )
    forge1.register_skill(skill)

    # Instantiate a second forge reading from the same storage directory
    forge2 = SkillForge(storage_dir=storage)
    assert forge2.get_case("c_disk") is not None
    assert forge2.get_skill("skill_sqlite_wal") is not None

    recalled = forge2.recall_skills("flush sqlite wal", domain="storage")
    assert len(recalled) == 1
    assert recalled[0].name == "SQLite WAL Checkpoint Sync"

