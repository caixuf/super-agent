"""Tests for the read-only aggregate memory health diagnostic."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = REPO_ROOT / "tools" / "memory_health.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("memory_health_under_test", CLI_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _identity(**overrides):
    value = {"profile": "profile-a", "user_id": "user-a", "agent_id": "agent-a"}
    value.update(overrides)
    return value


def _create_entity_db(
    path: Path,
    *,
    rules: list[tuple[str, float | str | None]] | None = None,
    entity_count: int | None = None,
    include_fts: bool = True,
) -> None:
    rules = rules if rules is not None else [("rule-mem-001", 100.0)]
    entity_count = entity_count if entity_count is not None else max(len(rules), 1)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE entities ("
            "entity_id TEXT PRIMARY KEY, name TEXT NOT NULL, aliases TEXT NOT NULL, "
            "category TEXT NOT NULL, summary TEXT NOT NULL, "
            "details TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE rules ("
            "rule_id TEXT PRIMARY KEY, entity_id TEXT NOT NULL, title TEXT NOT NULL, "
            "statement TEXT NOT NULL, polarity TEXT NOT NULL, keywords TEXT NOT NULL, "
            "updated_at REAL NOT NULL)"
        )
        if include_fts:
            connection.execute(
                "CREATE VIRTUAL TABLE entity_rules_fts USING fts5("
                "rule_id UNINDEXED, entity_name, aliases, title, statement, keywords)"
            )
        for index in range(entity_count):
            connection.execute(
                "INSERT INTO entities VALUES (?,?,?,?,?,?,?)",
                (f"ent-{index}", "entity", "[]", "test", "summary", "", 100.0),
            )
        for rule_id, updated_at in rules:
            entity_id = "ent-0"
            connection.execute(
                "INSERT INTO rules VALUES (?,?,?,?,?,?,?)",
                (rule_id, entity_id, "title", "statement", "reference", "keyword", updated_at),
            )
            if include_fts:
                connection.execute(
                    "INSERT INTO entity_rules_fts VALUES (?,?,?,?,?,?)",
                    (rule_id, "entity", "[]", "title", "statement", "keyword"),
                )


def _write_spool(path: Path, name: str, record: dict) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    target = path / name
    target.write_text(json.dumps(record), encoding="utf-8")
    return target


def _record(state: str, **identity):
    return {
        **_identity(**identity),
        "session_id": "session-not-reported",
        "turn_id": "turn-not-reported",
        "state": state,
        "occurred_at": 1000,
    }


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    return subprocess.run(
        [sys.executable, str(CLI_PATH), *args],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_missing_inputs_are_unknown_and_create_nothing(tmp_path):
    root = tmp_path / "absent"
    entity_db = root / "entity.db"
    memory_file = root / "MEMORY.md"
    spool_dir = root / "spool"

    proc = _run_cli(
        "--entity-db", str(entity_db),
        "--memory-file", str(memory_file),
        "--spool-dir", str(spool_dir),
        "--profile", "profile-a",
        "--user-id", "user-a",
        "--agent-id", "agent-a",
    )

    assert proc.returncode == 1
    result = json.loads(proc.stdout)
    assert result["status"] == "unknown"
    assert result["ok"] is False
    assert result["entity_index"]["error"] == "entity_db_missing"
    assert result["memory_source"]["error"] == "memory_file_missing"
    assert result["spool"]["error"] == "spool_dir_missing"
    assert result["worker_liveness"]["status"] == "not_inferred"
    assert not root.exists()


def test_corrupt_database_is_unhealthy_not_zero_healthy(tmp_path):
    module = _load_module()
    db = tmp_path / "corrupt.db"
    db.write_bytes(b"not a sqlite database")
    memory = tmp_path / "MEMORY.md"
    memory.write_text("metadata only", encoding="utf-8")
    spool = tmp_path / "spool"
    spool.mkdir()

    result = module.collect(
        entity_db=db,
        memory_file=memory,
        spool_dir=spool,
        **_identity(),
        now=10_000.0,
    )

    assert result["status"] == "unhealthy"
    assert result["entity_index"]["status"] == "unhealthy"
    assert result["entity_index"]["error"] == "entity_db_corrupt_or_unreadable"
    assert "counts" not in result["entity_index"]


def test_counts_sync_risk_and_specified_identity_isolation(tmp_path):
    module = _load_module()
    db = tmp_path / "entity.db"
    memory = tmp_path / "MEMORY.md"
    spool = tmp_path / "spool"
    _create_entity_db(
        db,
        rules=[("rule-mem-001", 100.0), ("rule-built-in", 200.0)],
        entity_count=3,
    )
    memory.write_text("source", encoding="utf-8")
    os.utime(memory, (250.0, 250.0))
    completed = _write_spool(spool, "a.json", _record("completed"))
    os.utime(completed, (100.0, 100.0))
    _write_spool(spool, "b.json", _record("completed", user_id="other-user"))
    _write_spool(spool, "c.json", _record("awaiting_verdict"))
    _write_spool(spool, "d.json", _record("enqueued"))
    _write_spool(spool, "e.json", _record("discarded"))

    result = module.collect(
        entity_db=db,
        memory_file=memory,
        spool_dir=spool,
        **_identity(),
        now=10_000.0,
    )

    assert result["entity_index"]["counts"] == {
        "entity_count": 3,
        "rule_count": 2,
        "indexed_rule_count": 2,
        "imported_rule_count": 1,
    }
    assert result["entity_index"]["max_updated_at"] == 200.0
    assert result["memory_source"]["source_mtime"] == 250.0
    assert result["sync"]["status"] == "possible_stale_index"
    assert result["sync"]["risk"] is True
    assert result["sync"]["memory_mtime_is_heuristic"] is True
    assert result["spool"]["identity_matched_records"] == 4
    assert result["spool"]["identity_mismatch_count"] == 1
    assert result["spool"]["state_counts"] == {
        "awaiting_verdict": 1,
        "completed": 1,
        "discarded": 1,
        "enqueued": 1,
    }
    assert result["spool"]["oldest_completed_age_seconds"] == pytest.approx(9_900.0)


def test_state_counts_bad_records_and_oldest_completed_age_are_deterministic(tmp_path):
    module = _load_module()
    db = tmp_path / "entity.db"
    memory = tmp_path / "MEMORY.md"
    spool = tmp_path / "spool"
    _create_entity_db(db, rules=[("rule-mem-001", 300.0)])
    memory.write_text("source", encoding="utf-8")
    os.utime(memory, (200.0, 200.0))
    completed = _write_spool(spool, "completed.json", _record("completed"))
    os.utime(completed, (900.0, 900.0))
    _write_spool(spool, "awaiting.json", _record("awaiting_verdict"))
    (spool / "broken.json").write_text("{not-json", encoding="utf-8")
    (spool / "array.json").write_text("[]", encoding="utf-8")
    _write_spool(spool, "missing-identity.json", {"state": "completed"})
    _write_spool(spool, "bad-state.json", _record("mystery"))
    (spool / "unreadable.json").symlink_to(spool / "does-not-exist.json")

    result = module.collect(
        entity_db=db,
        memory_file=memory,
        spool_dir=spool,
        **_identity(),
        now=1_000.0,
    )

    assert result["spool"]["files_scanned"] == 7
    assert result["spool"]["state_counts"]["completed"] == 1
    assert result["spool"]["state_counts"]["awaiting_verdict"] == 1
    assert result["spool"]["oldest_completed_age_seconds"] == pytest.approx(100.0)
    assert result["spool"]["invalid_json_count"] == 1
    assert result["spool"]["invalid_record_count"] == 3
    assert result["spool"]["unreadable_count"] == 1
    assert "spool_invalid_json" in result["spool"]["issues"]
    assert "spool_invalid_record" in result["spool"]["issues"]
    assert result["status"] == "degraded"


def test_output_never_contains_memory_record_or_exception_content(tmp_path):
    db = tmp_path / "entity.db"
    memory = tmp_path / "MEMORY.md"
    spool = tmp_path / "spool"
    secrets = (
        "PRIVATE_MEMORY_SENTINEL_7f31",
        "PRIVATE_RULE_SENTINEL_8e42",
        "PRIVATE_SPOOL_SENTINEL_9d53",
        "PRIVATE_EXCEPTION_SENTINEL_1a64",
    )
    _create_entity_db(db, rules=[("rule-mem-001", 100.0)])
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE rules SET title=?, statement=? WHERE rule_id='rule-mem-001'",
            (secrets[1], secrets[1]),
        )
    memory.write_text(secrets[0], encoding="utf-8")
    record = _record("completed")
    record["user_message"] = secrets[2]
    _write_spool(spool, "record.json", record)
    (spool / "invalid.json").write_text(secrets[3], encoding="utf-8")

    proc = _run_cli(
        "--entity-db", str(db),
        "--memory-file", str(memory),
        "--spool-dir", str(spool),
        "--profile", "private-profile-sentinel",
        "--user-id", "private-user-sentinel",
        "--agent-id", "private-agent-sentinel",
    )

    combined = proc.stdout + proc.stderr
    assert proc.returncode == 1
    for sentinel in secrets:
        assert sentinel not in combined
    assert "private-profile-sentinel" not in combined
    assert str(tmp_path) not in combined
    json.loads(proc.stdout)


def test_live_wal_database_and_wal_bytes_are_not_modified(tmp_path):
    module = _load_module()
    db = tmp_path / "live.db"
    memory = tmp_path / "MEMORY.md"
    spool = tmp_path / "spool"
    memory.write_text("source", encoding="utf-8")
    spool.mkdir()

    writer = sqlite3.connect(db)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE entities (entity_id TEXT)")
        writer.execute("CREATE TABLE rules (rule_id TEXT, updated_at REAL)")
        writer.execute("CREATE VIRTUAL TABLE entity_rules_fts USING fts5(rule_id)")
        writer.execute("INSERT INTO entities VALUES ('ent-0')")
        writer.execute("INSERT INTO rules VALUES ('rule-mem-001', 100.0)")
        writer.execute("INSERT INTO entity_rules_fts VALUES ('rule-mem-001')")
        writer.commit()

        wal = Path(str(db) + "-wal")
        shm = Path(str(db) + "-shm")
        assert wal.is_file() and shm.is_file()
        before_db = db.read_bytes()
        before_wal = wal.read_bytes()

        result = module.collect(
            entity_db=db,
            memory_file=memory,
            spool_dir=spool,
            **_identity(),
            now=1_000.0,
        )

        assert result["entity_index"]["counts"]["rule_count"] == 1
        assert db.read_bytes() == before_db
        assert wal.read_bytes() == before_wal
    finally:
        writer.close()


def test_missing_wal_shared_memory_is_unknown_without_creating_it(tmp_path):
    module = _load_module()
    db = tmp_path / "wal.db"
    memory = tmp_path / "MEMORY.md"
    spool = tmp_path / "spool"
    memory.write_text("source", encoding="utf-8")
    spool.mkdir()
    with sqlite3.connect(db) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE entities (entity_id TEXT)")
        connection.execute("CREATE TABLE rules (rule_id TEXT, updated_at REAL)")
        connection.execute("CREATE VIRTUAL TABLE entity_rules_fts USING fts5(rule_id)")
    wal = Path(str(db) + "-wal")
    shm = Path(str(db) + "-shm")
    wal.write_bytes(b"")
    if shm.exists():
        shm.unlink()

    result = module.collect(
        entity_db=db,
        memory_file=memory,
        spool_dir=spool,
        **_identity(),
        now=1_000.0,
    )

    assert result["entity_index"]["status"] == "unknown"
    assert result["entity_index"]["error"] == "entity_db_wal_shared_memory_missing"
    assert not shm.exists()


def test_symlinked_database_and_spool_are_resolved_and_read_without_sidecars(tmp_path):
    module = _load_module()
    actual = tmp_path / "actual"
    actual.mkdir()
    db = actual / "entity.db"
    spool = actual / "spool"
    _create_entity_db(db)
    spool.mkdir()
    _write_spool(spool, "record.json", _record("completed"))
    db_link = tmp_path / "entity-link.db"
    spool_link = tmp_path / "spool-link"
    db_link.symlink_to(db)
    spool_link.symlink_to(spool, target_is_directory=True)
    memory = actual / "MEMORY.md"
    memory.write_text("source", encoding="utf-8")
    memory_link = tmp_path / "MEMORY-link.md"
    memory_link.symlink_to(memory)

    result = module.collect(
        entity_db=db_link,
        memory_file=memory_link,
        spool_dir=spool_link,
        **_identity(),
        now=1_000.0,
    )

    assert result["entity_index"]["status"] == "healthy"
    assert result["spool"]["state_counts"]["completed"] == 1
    assert not Path(str(db_link) + "-wal").exists()
    assert not Path(str(db_link) + "-shm").exists()


def test_non_wal_database_read_does_not_create_sidecars(tmp_path):
    module = _load_module()
    db = tmp_path / "plain.db"
    memory = tmp_path / "MEMORY.md"
    spool = tmp_path / "spool"
    _create_entity_db(db)
    memory.write_text("source", encoding="utf-8")
    spool.mkdir()

    result = module.collect(
        entity_db=db,
        memory_file=memory,
        spool_dir=spool,
        **_identity(),
        now=1_000.0,
    )

    assert result["entity_index"]["status"] == "healthy"
    assert not Path(str(db) + "-wal").exists()
    assert not Path(str(db) + "-shm").exists()


def test_sqlite_is_always_opened_mode_ro(monkeypatch, tmp_path):
    module = _load_module()
    db = tmp_path / "entity.db"
    _create_entity_db(db)
    calls = []
    real_connect = sqlite3.connect

    def recording_connect(database, *args, **kwargs):
        calls.append((database, args, kwargs))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(module.sqlite3, "connect", recording_connect)
    result = module._inspect_entity_db(db)

    assert result["status"] == "healthy"
    assert calls
    assert all("mode=ro" in call[0] for call in calls)
    assert all(call[2].get("uri") is True for call in calls)


def test_healthy_snapshot_and_no_worker_inference(tmp_path):
    module = _load_module()
    db = tmp_path / "entity.db"
    memory = tmp_path / "MEMORY.md"
    spool = tmp_path / "spool"
    _create_entity_db(db, rules=[("rule-mem-001", 200.0)])
    memory.write_text("source", encoding="utf-8")
    os.utime(memory, (100.0, 100.0))
    spool.mkdir()

    result = module.collect(
        entity_db=db,
        memory_file=memory,
        spool_dir=spool,
        **_identity(),
        now=1_000.0,
    )

    assert result["status"] == "healthy"
    assert result["ok"] is True
    assert result["issues"] == []
    assert result["sync"]["risk"] is False
    assert result["spool"]["state_counts"] == {
        "awaiting_verdict": 0,
        "completed": 0,
        "discarded": 0,
        "enqueued": 0,
    }
    assert result["worker_liveness"] == {
        "status": "not_inferred",
        "reason": "spool_records_do_not_prove_worker_process_state",
    }


def test_argument_errors_are_json_and_do_not_echo_values(capsys):
    module = _load_module()
    sensitive = "PRIVATE_ARGUMENT_SENTINEL"

    with pytest.raises(SystemExit) as raised:
        module.main(["--entity-db", sensitive])

    assert raised.value.code == 2
    output = capsys.readouterr().out
    assert sensitive not in output
    result = json.loads(output)
    assert result["status"] == "unknown"
    assert result["error"] == "invalid_arguments"


def test_pending_completed_turns_are_degraded_without_inventing_worker_state(tmp_path):
    module = _load_module()
    db = tmp_path / "entity.db"
    memory = tmp_path / "MEMORY.md"
    spool = tmp_path / "spool"
    _create_entity_db(db, rules=[("rule-mem-001", 200.0)])
    memory.write_text("source", encoding="utf-8")
    os.utime(memory, (100.0, 100.0))
    _write_spool(spool, "turn.json", _record("completed"))
    result = module.collect(entity_db=db, memory_file=memory, spool_dir=spool,
                            **_identity(), now=1_000.0)
    assert result["status"] == "degraded"
    assert "completed_turns_pending_ingestion" in result["spool"]["issues"]
    assert result["worker_liveness"]["status"] == "not_inferred"
