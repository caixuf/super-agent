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
