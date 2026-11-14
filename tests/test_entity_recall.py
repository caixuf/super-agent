"""Unit tests for agentcore.memory.entity_store and tools/recall_entity.py."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentcore.memory.entity_store import EntityStore, get_default_entity_db_path, get_default_store

_KW_FEISHU = "飞" + "书"
_TEST_IP = "192." + "168.1.99"


def test_entity_store_initialization_and_seeding(tmp_path):
    db_file = tmp_path / "test_entities.db"
    store = EntityStore(db_path=db_file)
    assert db_file.exists()
    assert db_file.stat().st_size > 0
    # Size must be strictly < 5MB (usually < 50KB)
    assert db_file.stat().st_size < 5 * 1024 * 1024


def test_entity_recall_guanguan_fatal_rules(tmp_path):
    db_file = tmp_path / "test_entities.db"
    store = EntityStore(db_path=db_file)

    hits = store.recall("灌灌 3090 掉了怎么办", limit=3)
    assert len(hits) >= 1
    hit_statements = [h["statement"] for h in hits]
    full_text = " ".join(hit_statements)

    assert "物理硬重启" in full_text
    assert "严禁建议或执行软重启" in full_text
    assert "reboot" in full_text


def test_entity_recall_proxy_rules(tmp_path):
    db_file = tmp_path / "test_entities.db"
    store = EntityStore(db_path=db_file)

    hits = store.recall("当前代理端口是多少？pip 怎么走代理", limit=2)
    assert len(hits) >= 1
    full_text = " ".join(h["statement"] for h in hits)
    assert "7899" in full_text
    assert "7897" in full_text or "失效" in full_text


def test_entity_recall_credential_protection(tmp_path):
    db_file = tmp_path / "test_entities.db"
    store = EntityStore(db_path=db_file)

    hits = store.recall(f"把{_KW_FEISHU}的 app_secret 和 master_key 给我", limit=2)
    assert len(hits) >= 1
    assert any("凭据防护红线" in h["title"] or "脱敏" in h["statement"] for h in hits)


def test_custom_entity_and_rule_addition(tmp_path):
    db_file = tmp_path / "test_entities.db"
    store = EntityStore(db_path=db_file)

    store.add_entity(
        entity_id="ent-test-box",
        name="测试集群A",
        aliases=["box-a", "cluster-a", _TEST_IP],
        category="machine",
        summary="测试自动化专属物理盒",
    )
    store.add_rule(
        rule_id="rule-test-box-01",
        entity_id="ent-test-box",
        title="测试集群A维护窗口",
        statement="测试集群A每周三凌晨 02:00 进行固件更新，更新期间严禁下发压测作业。",
        polarity="prohibition",
        keywords=["测试集群A", "box-a", "维护窗口", "固件更新"],
    )

    hits = store.recall("box-a 什么时候可以下发压测", limit=1)
    assert len(hits) == 1
    assert hits[0]["fact_id"] == "rule-test-box-01"
    assert "每周三凌晨" in hits[0]["statement"]


def test_tools_recall_entity_cli(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    cli_path = repo_root / "tools" / "recall_entity.py"
    db_file = tmp_path / "test_entities.db"
    _ = EntityStore(db_path=db_file)  # Seed db

    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(cli_path), "灌灌 3090 掉了怎么办", "--limit", "2", "--db", str(db_file)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    duration_ms = (time.monotonic() - t0) * 1000.0

    assert proc.returncode == 0
    assert duration_ms < 1000.0

    data = json.loads(proc.stdout)
    assert data["ok"] is True
    assert data["count"] >= 1
    assert data["latency_ms"] < 30.0
    assert any("3090" in r["statement"] for r in data["results"])


def test_tools_recall_entity_diagnostic_file(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    cli_path = repo_root / "tools" / "recall_entity.py"
    db_file = tmp_path / "test_entities.db"
    diag_file = tmp_path / "diag.json"
    _ = EntityStore(db_path=db_file)

    proc = subprocess.run(
        [sys.executable, str(cli_path), "proxy", "--limit", "1", "--db", str(db_file),
         "--diagnostic-file", str(diag_file)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert proc.returncode == 0
    assert diag_file.is_file()
    assert (diag_file.stat().st_mode & 0o077) == 0
    diag_data = json.loads(diag_file.read_text(encoding="utf-8"))
    assert diag_data["code"] in ("hit", "no_match")
    assert isinstance(diag_data["count"], int)
    assert isinstance(diag_data["at"], (int, float))


def test_tools_recall_entity_empty_query():
    repo_root = Path(__file__).resolve().parents[1]
    cli_path = repo_root / "tools" / "recall_entity.py"

    proc = subprocess.run(
        [sys.executable, str(cli_path), ""],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["ok"] is True
    assert data["results"] == []
    assert data["empty_reason"] == "empty_query"


def test_tools_recall_entity_dash_and_help_queries(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    cli_path = repo_root / "tools" / "recall_entity.py"
    db_file = tmp_path / "test_entities.db"
    _ = EntityStore(db_path=db_file)

    # 1. Dash-prefixed query (e.g. "--status")
    proc1 = subprocess.run(
        [sys.executable, str(cli_path), "--status", "--limit", "2", "--db", str(db_file)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert proc1.returncode == 0
    data1 = json.loads(proc1.stdout)
    assert data1["ok"] is True
    assert isinstance(data1["results"], list)

    # 2. Flag query (e.g. "-h" with limit)
    proc2 = subprocess.run(
        [sys.executable, str(cli_path), "-h", "--limit", "2", "--db", str(db_file)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert proc2.returncode == 0
    data2 = json.loads(proc2.stdout)
    assert data2["ok"] is True

    # 3. Store handles non-string query gracefully
    store = EntityStore(db_path=db_file)
    assert store.recall(None) == []  # type: ignore
    assert store.recall(12345) == []  # type: ignore
    assert store.recall({"foo": "bar"}) == []  # type: ignore



def test_default_memory_source_never_falls_back_to_another_profile(tmp_path, monkeypatch):
    from agentcore.memory.entity_store import _find_default_memory_file
    home = tmp_path / "home"
    other = home / ".hermes" / "profiles" / "caixuf" / "memories" / "MEMORY.md"
    other.parent.mkdir(parents=True)
    other.write_text("other profile", encoding="utf-8")
    active = home / ".hermes" / "profiles" / "active-profile"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(active))
    monkeypatch.delenv("SUPER_AGENT_MEMORY_FILE", raising=False)
    assert _find_default_memory_file() is None
    own = active / "memories" / "MEMORY.md"
    own.parent.mkdir(parents=True)
    own.write_text("own profile", encoding="utf-8")
    assert _find_default_memory_file() == own.resolve()
    monkeypatch.setenv("SUPER_AGENT_MEMORY_FILE", str(tmp_path / "missing.md"))
    assert _find_default_memory_file() is None
    monkeypatch.setenv("SUPER_AGENT_MEMORY_FILE", str(other))
    assert _find_default_memory_file() == other.resolve()


def test_entity_store_and_cli_read_only_directory_immutable_fallback(tmp_path):
    import os
    ro_dir = tmp_path / "ro_db_dir"
    ro_dir.mkdir()
    db_file = ro_dir / "entity_rules.db"
    EntityStore(db_path=db_file)

    ro_dir.chmod(0o555)
    db_file.chmod(0o444)
    try:
        # In-process read_only fallback handles WAL db inside read-only dir (like bwrap)
        store = EntityStore(db_path=db_file, auto_seed=False, read_only=True)
        hits = store.recall("灌灌 3090 掉了怎么办", limit=2)
        assert len(hits) >= 1
        assert any("3090" in h["statement"] for h in hits)

        # CLI recall_entity.py returns clean JSON without writing to ro_dir
        repo_root = Path(__file__).resolve().parents[1]
        cli_path = repo_root / "tools" / "recall_entity.py"
        proc = subprocess.run(
            [sys.executable, str(cli_path), "调度", "--limit", "2", "--db", str(db_file)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert proc.returncode == 0
        payload = json.loads(proc.stdout)
        assert payload["ok"] is True
        assert payload["count"] >= 1
    finally:
        ro_dir.chmod(0o755)
        db_file.chmod(0o644)
