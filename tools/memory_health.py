#!/usr/bin/env python3
"""Read-only memory health diagnostics.

This command deliberately reports only aggregate metadata. It never prints
memory text, spool records, identities, file names, exception messages, or
credentials. It does not import agentcore configuration or storage classes,
and it never creates a directory, database, lock, or spool file.

The spool is evidence about durable records, not evidence that a worker
process is running. Worker liveness is therefore always reported as
``not_inferred``.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Dict, Iterable, Optional


SCHEMA = "agentcore.memory_health.v1"
IDENTITY_FIELDS = ("profile", "user_id", "agent_id")
SPOOL_STATES = ("awaiting_verdict", "completed", "enqueued", "discarded")


class _JsonArgumentParser(argparse.ArgumentParser):
    """Keep argument failures machine-readable without echoing user input."""

    def error(self, message: str) -> None:  # noqa: ARG002 - never echo it
        json.dump(
            {
                "schema": SCHEMA,
                "ok": False,
                "status": "unknown",
                "error": "invalid_arguments",
                "worker_liveness": "not_inferred",
            },
            sys.stdout,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        sys.stdout.write("\n")
        raise SystemExit(2)


def _readonly_connection(path: Path) -> sqlite3.Connection:
    """Open the resolved SQLite file without permitting writes.

    ``mode=ro`` is mandatory. For a non-WAL database ``immutable=1`` prevents
    SQLite from creating empty ``-wal``/``-shm`` sidecars merely by reading it.
    A WAL database is opened with ordinary ``mode=ro`` so the committed WAL is
    visible; DB and WAL bytes remain read-only. If the WAL has no existing
    shared-memory sidecar, the caller refuses instead of creating one.
    """
    path = path.expanduser().resolve(strict=True)
    wal = Path(str(path) + "-wal")
    shm = Path(str(path) + "-shm")
    if wal.exists() and not shm.exists():
        raise RuntimeError("wal_read_requires_existing_shm")
    query = "?mode=ro&immutable=1" if not wal.exists() else "?mode=ro"
    connection = sqlite3.connect(
        path.as_uri() + query,
        uri=True,
        timeout=2.0,
    )
    connection.execute("PRAGMA query_only = ON")
    return connection


def _healthy(details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    value: Dict[str, Any] = {"status": "healthy", "issues": []}
    if details:
        value.update(details)
    return value


def _unknown(error: str) -> Dict[str, Any]:
    return {"status": "unknown", "error": error, "issues": [error]}


def _unhealthy(error: str) -> Dict[str, Any]:
    return {"status": "unhealthy", "error": error, "issues": [error]}


def _inspect_memory_source(memory_file: Path) -> Dict[str, Any]:
    """Inspect metadata only; memory text is never opened."""
    try:
        if not memory_file.exists():
            return _unknown("memory_file_missing")
        memory_file = memory_file.expanduser().resolve(strict=True)
        if not memory_file.is_file():
            return _unhealthy("memory_file_not_regular")
        stat = memory_file.stat()
        if stat.st_size == 0:
            return _unhealthy("memory_file_empty")
        return _healthy(
            {
                "source_mtime": float(stat.st_mtime),
                "source_size_bytes": int(stat.st_size),
                "mtime_basis": "filesystem_metadata_heuristic",
            }
        )
    except (OSError, ValueError):
        return _unknown("memory_file_unreadable")


def _inspect_entity_db(entity_db: Path) -> Dict[str, Any]:
    """Return aggregate counts and timestamps from an existing entity DB."""
    try:
        if not entity_db.exists():
            return _unknown("entity_db_missing")
        entity_db = entity_db.expanduser().resolve(strict=True)
        if not entity_db.is_file():
            return _unhealthy("entity_db_not_regular")
    except (OSError, ValueError):
        return _unknown("entity_db_unreadable")

    connection: Optional[sqlite3.Connection] = None
    try:
        connection = _readonly_connection(entity_db)
        integrity = connection.execute("PRAGMA quick_check").fetchall()
        if integrity != [("ok",)]:
            return _unhealthy("entity_db_integrity_failed")

        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        missing = [name for name in ("entities", "rules") if name not in tables]
        if missing:
            return _unhealthy("entity_db_schema_incomplete")

        entity_count = int(
            connection.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        )
        rule_row = connection.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN typeof(updated_at) IN ('integer', 'real') THEN 0 ELSE 1 END), "
            "MAX(CASE WHEN typeof(updated_at) IN ('integer', 'real') THEN updated_at END), "
            "SUM(CASE WHEN instr(CAST(rule_id AS TEXT), 'rule-mem-') = 1 THEN 1 ELSE 0 END) "
            "FROM rules"
        ).fetchone()
        rule_count = int(rule_row[0] or 0)
        invalid_updated_at_count = int(rule_row[1] or 0)
        max_updated_at = (
            float(rule_row[2]) if rule_row[2] is not None else None
        )
        imported_rule_count = int(rule_row[3] or 0)

        issues = []
        fts_status = "healthy"
        indexed_rule_count: Optional[int] = rule_count
        if "entity_rules_fts" not in tables:
            fts_status = "degraded"
            indexed_rule_count = None
            issues.append("entity_rules_fts_missing")
        else:
            try:
                indexed_rule_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM entity_rules_fts"
                    ).fetchone()[0]
                )
                if indexed_rule_count != rule_count:
                    issues.append("entity_rules_fts_count_mismatch")
            except sqlite3.Error:
                fts_status = "degraded"
                indexed_rule_count = None
                issues.append("entity_rules_fts_unreadable")

        if rule_count == 0:
            issues.append("no_rules_indexed")
        if invalid_updated_at_count:
            issues.append("rule_updated_at_invalid")
        if imported_rule_count == 0:
            issues.append("no_imported_rules")
        if max_updated_at is None and rule_count:
            issues.append("rule_updated_at_missing")

        status = "healthy" if not issues else "degraded"
        return {
            "status": status,
            "quick_check": "ok",
            "issues": issues,
            "counts": {
                "entity_count": entity_count,
                "rule_count": rule_count,
                "indexed_rule_count": indexed_rule_count,
                "imported_rule_count": imported_rule_count,
            },
            "fts_status": fts_status,
            "invalid_updated_at_count": invalid_updated_at_count,
            "max_updated_at": max_updated_at,
            "imported_rule_count_basis": "rule_id_prefix_rule-mem-",
        }
    except RuntimeError:
        return _unknown("entity_db_wal_shared_memory_missing")
    except (OSError, ValueError):
        return _unknown("entity_db_unreadable")
    except sqlite3.DatabaseError:
        return _unhealthy("entity_db_corrupt_or_unreadable")
    finally:
        if connection is not None:
            connection.close()


def _sync_assessment(
    entity_index: Dict[str, Any],
    memory_source: Dict[str, Any],
) -> Dict[str, Any]:
    """Compare memory mtime with index recency, without claiming provenance."""
    result: Dict[str, Any] = {
        "status": "unknown",
        "risk": None,
        "delta_seconds": None,
        "memory_mtime_is_heuristic": True,
        "basis": "memory_source_mtime_vs_max_rules_updated_at",
    }
    source_mtime = memory_source.get("source_mtime")
    max_updated_at = entity_index.get("max_updated_at")
    counts = entity_index.get("counts")

    if not isinstance(source_mtime, (int, float)):
        result["reason"] = "memory_source_unavailable"
        return result
    if not isinstance(max_updated_at, (int, float)):
        result["reason"] = "index_timestamp_unavailable"
        return result
    if not isinstance(counts, dict):
        result["reason"] = "index_counts_unavailable"
        return result

    delta = round(float(source_mtime) - float(max_updated_at), 3)
    result["delta_seconds"] = delta
    result["mtime_newer_than_index"] = delta > 0
    if int(counts.get("imported_rule_count") or 0) == 0:
        result.update(status="no_imported_rules", risk=True)
    elif delta > 0:
        result.update(status="possible_stale_index", risk=True)
    else:
        result.update(status="source_mtime_not_newer", risk=False)
    return result


def _load_spool_record(path: Path) -> tuple[str, Optional[Dict[str, Any]]]:
    """Read one record and return a bounded classification plus parsed data."""
    try:
        if path.is_symlink() or not path.is_file():
            return "unreadable", None
        raw = path.read_bytes()
    except OSError:
        return "unreadable", None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "invalid_json", None
    if not isinstance(value, dict):
        return "invalid_record", None
    return "json_object", value


def _identity_is_well_formed(record: Dict[str, Any]) -> bool:
    return all(
        isinstance(record.get(field), str) and bool(record.get(field).strip())
        for field in IDENTITY_FIELDS
    )


def _inspect_spool(
    spool_dir: Path,
    identity: Dict[str, str],
    *,
    now: float,
) -> Dict[str, Any]:
    try:
        if not spool_dir.exists():
            return _unknown("spool_dir_missing")
        spool_dir = spool_dir.expanduser().resolve(strict=True)
        if not spool_dir.is_dir():
            return _unhealthy("spool_dir_not_directory")
        paths = sorted(spool_dir.glob("*.json"))
    except (OSError, ValueError):
        return _unknown("spool_dir_unreadable")

    states = {name: 0 for name in SPOOL_STATES}
    invalid_json_count = 0
    invalid_record_count = 0
    identity_mismatch_count = 0
    unreadable_count = 0
    valid_identity_records = 0
    oldest_completed_age: Optional[float] = None
    oldest_completed_mtime: Optional[float] = None

    for path in paths:
        classification, record = _load_spool_record(path)
        if classification == "invalid_json":
            invalid_json_count += 1
            continue
        if classification in ("invalid_record", "unreadable"):
            if classification == "unreadable":
                unreadable_count += 1
            else:
                invalid_record_count += 1
            continue

        assert record is not None
        if not _identity_is_well_formed(record):
            invalid_record_count += 1
            continue
        if any(record.get(field) != identity[field] for field in IDENTITY_FIELDS):
            identity_mismatch_count += 1
            continue

        state = record.get("state")
        if not isinstance(state, str) or state not in states:
            invalid_record_count += 1
            continue

        valid_identity_records += 1
        states[state] += 1
        if state == "completed":
            try:
                file_mtime = float(path.stat().st_mtime)
            except (OSError, ValueError):
                unreadable_count += 1
                continue
            age = max(0.0, round(now - file_mtime, 3))
            if oldest_completed_age is None or age > oldest_completed_age:
                oldest_completed_age = age
                oldest_completed_mtime = file_mtime

    issues = []
    if states["completed"]:
        issues.append("completed_turns_pending_ingestion")
    if invalid_json_count:
        issues.append("spool_invalid_json")
    if invalid_record_count:
        issues.append("spool_invalid_record")
    if identity_mismatch_count:
        issues.append("spool_identity_mismatch")
    if unreadable_count:
        issues.append("spool_record_unreadable")

    status = "healthy" if not issues else "degraded"
    return {
        "status": status,
        "issues": issues,
        "files_scanned": len(paths),
        "identity_scope": "specified",
        "identity_matched_records": valid_identity_records,
        "state_counts": states,
        "invalid_json_count": invalid_json_count,
        "invalid_record_count": invalid_record_count,
        "identity_mismatch_count": identity_mismatch_count,
        "unreadable_count": unreadable_count,
        "oldest_completed_age_seconds": oldest_completed_age,
        "oldest_completed_mtime": oldest_completed_mtime,
        "completed_age_basis": "record_file_mtime_heuristic",
    }


def _overall_status(components: Iterable[Dict[str, Any]], sync: Dict[str, Any]) -> str:
    statuses = [str(component.get("status")) for component in components]
    statuses.append(str(sync.get("status")))
    if "unhealthy" in statuses:
        return "unhealthy"
    if "unknown" in statuses:
        return "unknown"
    if "degraded" in statuses or sync.get("risk") is True:
        return "degraded"
    return "healthy"


def collect(
    *,
    entity_db: Path,
    memory_file: Path,
    spool_dir: Path,
    profile: str,
    user_id: str,
    agent_id: str,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Collect a non-mutating health snapshot from explicit paths only."""
    observed_now = float(time.time() if now is None else now)
    memory_source = _inspect_memory_source(memory_file)
    entity_index = _inspect_entity_db(entity_db)
    sync = _sync_assessment(entity_index, memory_source)
    spool = _inspect_spool(
        spool_dir,
        {"profile": profile, "user_id": user_id, "agent_id": agent_id},
        now=observed_now,
    )

    components = (memory_source, entity_index, spool)
    status = _overall_status(components, sync)
    issues = set()
    for component in components:
        for issue in component.get("issues", []):
            if isinstance(issue, str):
                issues.add(issue)
    for issue in sync.get("issues", []):
        if isinstance(issue, str):
            issues.add(issue)
    if sync.get("risk") is True:
        issues.add("possible_memory_index_sync_risk")

    return {
        "schema": SCHEMA,
        "observed_at": datetime.fromtimestamp(
            observed_now, tz=timezone.utc
        ).isoformat(timespec="seconds"),
        "ok": status == "healthy",
        "status": status,
        "issues": sorted(issues),
        "memory_source": memory_source,
        "entity_index": entity_index,
        "sync": sync,
        "spool": spool,
        "worker_liveness": {
            "status": "not_inferred",
            "reason": "spool_records_do_not_prove_worker_process_state",
        },
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = _JsonArgumentParser(
        description="Read-only aggregate health diagnostics for memory artifacts."
    )
    parser.add_argument("--entity-db", required=True, type=Path)
    parser.add_argument("--memory-file", required=True, type=Path)
    parser.add_argument("--spool-dir", required=True, type=Path)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--agent-id", required=True)
    args = parser.parse_args(argv)

    try:
        result = collect(
            entity_db=args.entity_db,
            memory_file=args.memory_file,
            spool_dir=args.spool_dir,
            profile=args.profile,
            user_id=args.user_id,
            agent_id=args.agent_id,
        )
    except Exception:
        # Never include exception text: it can contain record or path content.
        result = {
            "schema": SCHEMA,
            "ok": False,
            "status": "unknown",
            "error": "diagnostic_internal_error",
            "worker_liveness": "not_inferred",
        }

    json.dump(
        result,
        sys.stdout,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    sys.stdout.write("\n")
    return 0 if result.get("status") == "healthy" else 1


if __name__ == "__main__":
    raise SystemExit(main())
