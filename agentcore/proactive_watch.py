"""Proactive observation — the agent looks, records what it *would* do, and stays quiet.

Why this exists
---------------
Everything in this repository up to now answered a request. Nothing ever looked
outside on its own: the only initiative in the deployment was one nightly cron
job. A system that only reacts cannot notice a task that finished without its
evidence, or a worktree that has been dirty for days.

This module is the smallest honest form of initiative:

* it **observes** a fixed set of conditions;
* it writes down, in plain words, **what it would have done** about each one;
* and in ``observe`` mode it **does not do it** — no message, no edit, no command.

That is deliberate. Initiative without a record of its own judgement is not
something a reader can audit or stop. ``auto`` exists in the type system so the
switch has a name, but this module never sends anything on its own: delivering a
finding is a separate, explicit step the operator takes.

No third-party imports: the package import-boundary gate forbids them.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

MODES = frozenset({"off", "observe", "auto"})
DEFAULT_DB_PATH = Path.home() / ".local" / "state" / "agentcore" / "proactive.db"

_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS findings (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          REAL NOT NULL,
  watcher     TEXT NOT NULL,
  subject     TEXT NOT NULL DEFAULT '',
  observation TEXT NOT NULL DEFAULT '',
  intent      TEXT NOT NULL DEFAULT '',
  severity    TEXT NOT NULL DEFAULT 'info',
  mode        TEXT NOT NULL DEFAULT 'observe',
  acted       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_findings_watcher ON findings(watcher, ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_findings_once
  ON findings(watcher, subject, observation);
"""


def default_db_path() -> Path:
    raw = os.environ.get("AGENTCORE_PROACTIVE_DB", "").strip()
    if raw:
        return Path(raw).expanduser()
    state = os.environ.get("AGENTCORE_STATE_DIR", "").strip()
    if state:
        return Path(state).expanduser() / "proactive.db"
    return DEFAULT_DB_PATH

# --------------------------------------------------------------------------- #
# Watchers: each reads real state and returns findings as plain sentences
# --------------------------------------------------------------------------- #

def _ledger_rows(ledger, state: str) -> List[Dict[str, Any]]:
    if ledger is None:
        return []
    try:
        rows = ledger.list_tasks(active_only=False)
    except (AttributeError, TypeError):
        return []
    return [row for row in rows if str(row.get("state") or "") == state]


def watch_unverified(ledger, *, stale_seconds: float = 1800.0,
                     max_age_seconds: float = 86400.0,
                     now: Optional[float] = None) -> List[Dict[str, str]]:
    """Tasks that claimed completion but still cannot show their evidence.

    Only recent ones: an old row is history, not something to raise again today.
    """
    now = time.time() if now is None else now
    out: List[Dict[str, str]] = []
    for row in _ledger_rows(ledger, "unverified"):
        age = now - float(row.get("updated_at") or now)
        if age < stale_seconds or age > max_age_seconds:
            continue
        label = str(row.get("label") or row.get("task_id") or "该任务")
        out.append({
            "subject": str(row.get("task_id") or ""),
            "observation": "任务声称完成但验收条件仍未满足，已等待 %d 分钟" % int(age / 60),
            "intent": "提醒你验收「%s」，或补上它缺少的产物" % label,
            "severity": "warn",
        })
    return out


def watch_stalled(ledger, *, stale_seconds: float = 3600.0,
                  max_age_seconds: float = 86400.0,
                  now: Optional[float] = None) -> List[Dict[str, str]]:
    """Tasks whose runner went silent or vanished — summarised, not listed.

    Twenty dead rows are one problem, not twenty. The finding says how many and
    names a few, so the record stays readable and the intent stays actionable.
    """
    now = time.time() if now is None else now
    buckets: Dict[str, List[str]] = {"stalled": [], "lost": []}
    for state in ("stalled", "lost"):
        for row in _ledger_rows(ledger, state):
            age = now - float(row.get("heartbeat_at") or now)
            if age < stale_seconds or age > max_age_seconds:
                continue
            buckets[state].append(str(row.get("label") or row.get("task_id") or "该任务"))
    out: List[Dict[str, str]] = []
    for state, labels in buckets.items():
        if not labels:
            continue
        sample = "、".join(labels[:3])
        more = "等 %d 个" % len(labels) if len(labels) > 3 else ""
        out.append({
            "subject": "state:%s" % state,
            "observation": "最近一天有 %d 个任务处于「%s」没有响应" % (len(labels), state),
            "intent": "确认这些任务是真停了还是只是没回执，然后清理账本（%s%s）"
                      % (sample, more),
            "severity": "warn",
        })
    return out


def watch_dirty_repo(repo: Path, *, stale_seconds: float = 7200.0,
                     now: Optional[float] = None,
                     runner: Optional[Callable[..., Any]] = None) -> List[Dict[str, str]]:
    """A worktree carrying uncommitted changes for longer than a work session."""
    now = time.time() if now is None else now
    runner = runner or subprocess.run
    try:
        proc = runner(["git", "-C", str(repo), "status", "--porcelain"],
                      capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    if getattr(proc, "returncode", 1) != 0:
        return []
    lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    if not lines:
        return []
    newest = 0.0
    try:
        mtimes = []
        for line in lines[:200]:
            candidate = repo / line[3:].strip()
            if candidate.exists():
                mtimes.append(candidate.stat().st_mtime)
        if mtimes:
            newest = max(mtimes)
    except OSError:
        newest = 0.0
    changed_age = now - newest if newest else float("inf")
    if changed_age < stale_seconds:
        return []
    return [{
        "subject": str(repo),
        "observation": "仓库有 %d 个未提交改动，最近一次改动在 %d 分钟前"
                       % (len(lines), int(min(changed_age, 59999) / 60)),
        "intent": "把 %d 个改动整理成一次提交，或说明为什么先不提交" % len(lines),
        "severity": "info",
    }]


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #

class FindingStore:
    """Append-only record of what the agent noticed and what it would have done."""

    def __init__(self, path: Optional[Any] = None):
        self.path = Path(path) if path is not None else default_db_path()

    def ensure_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(_SCHEMA)

    def _db(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def record(self, findings: Sequence[Dict[str, str]], *, mode: str = "observe",
               now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Store new findings; a repeated (watcher, subject, observation) is skipped."""
        if mode not in MODES:
            raise ValueError("mode must be one of " + ", ".join(sorted(MODES)))
        now = time.time() if now is None else now
        stored: List[Dict[str, Any]] = []
        self.ensure_schema()
        with self._db() as db:
            for item in findings:
                watcher = str(item.get("watcher") or "")
                if not watcher:
                    continue
                try:
                    cur = db.execute(
                        "INSERT OR IGNORE INTO findings"
                        " (ts, watcher, subject, observation, intent, severity, mode)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (now, watcher, str(item.get("subject") or ""),
                         str(item.get("observation") or ""), str(item.get("intent") or ""),
                         str(item.get("severity") or "info"), mode))
                except sqlite3.Error:
                    continue
                if cur.rowcount:
                    stored.append(dict(item, watcher=watcher, mode=mode))
        return stored

    def recent(self, *, limit: int = 20, watcher: Optional[str] = None) -> List[Dict[str, Any]]:
        self.ensure_schema()
        sql = "SELECT * FROM findings"
        params: List[Any] = []
        if watcher:
            sql += " WHERE watcher=?"
            params.append(watcher)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self._db() as db:
            return [dict(row) for row in db.execute(sql, params).fetchall()]

    def counts(self) -> Dict[str, int]:
        self.ensure_schema()
        with self._db() as db:
            rows = db.execute(
                "SELECT watcher, COUNT(*) AS n FROM findings GROUP BY watcher").fetchall()
        return {str(row["watcher"]): int(row["n"]) for row in rows}


# --------------------------------------------------------------------------- #
# One pass
# --------------------------------------------------------------------------- #

def run_pass(*, store: FindingStore, ledger=None, repo: Optional[Path] = None,
             mode: str = "observe", now: Optional[float] = None,
             watchers: Optional[Dict[str, Callable[[], List[Dict[str, str]]]]] = None) -> Dict[str, Any]:
    """Observe every watcher once and record what the agent would have done.

    ``off`` runs nothing. ``observe`` stores findings and delivers nothing.
    ``auto`` is accepted by the type but deliberately behaves like ``observe``
    here: sending is a separate decision, never a silent side effect of a
    scheduled check.
    """
    if mode not in MODES:
        raise ValueError("mode must be one of " + ", ".join(sorted(MODES)))
    if mode == "off":
        return {"mode": mode, "ran": False, "stored": [], "considered": 0, "errors": {}}

    active = watchers if watchers is not None else {
        "unverified": lambda: watch_unverified(ledger, now=now),
        "stalled": lambda: watch_stalled(ledger, now=now),
        "dirty_repo": lambda: watch_dirty_repo(repo, now=now) if repo else [],
    }
    collected: List[Dict[str, str]] = []
    errors: Dict[str, str] = {}
    for name, fn in active.items():
        try:
            for item in fn() or ():
                collected.append(dict(item, watcher=name))
        except Exception as error:      # one broken watcher must not hide the others
            errors[name] = "%s: %s" % (type(error).__name__, error)
    stored = store.record(collected, mode=mode, now=now)
    return {"mode": mode, "ran": True, "considered": len(collected),
            "stored": stored, "errors": errors}


# --------------------------------------------------------------------------- #
# Command line — what the timer runs
# --------------------------------------------------------------------------- #

def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    import sys

    from agentcore.liveness import LivenessLedger

    parser = argparse.ArgumentParser(
        prog="proactive_watch",
        description="观察档：看一圈，只记录「本来会做什么」，不发消息不动手。")
    parser.add_argument("--mode", default=os.environ.get("AGENTCORE_PROACTIVE_MODE", "observe"),
                        choices=sorted(MODES))
    parser.add_argument("--repo", action="append", default=[],
                        help="要观察的仓库路径，可多次指定")
    parser.add_argument("--db", default="", help="发现记录库路径")
    parser.add_argument("--liveness-db", default="", help="任务账本路径")
    parser.add_argument("--print", dest="show", action="store_true", help="打印本次记录")
    args = parser.parse_args(list(argv) if argv is not None else None)

    store = FindingStore(Path(args.db).expanduser() if args.db else None)
    ledger = LivenessLedger(Path(args.liveness_db).expanduser()) if args.liveness_db else None
    repo = Path(args.repo[0]).expanduser() if args.repo else None

    result = run_pass(store=store, ledger=ledger, repo=repo, mode=args.mode)
    if args.show:
        for item in result.get("stored") or ():
            print("[%s] %s — %s" % (item.get("watcher"), item.get("observation"), item.get("intent")))
        if result.get("errors"):
            for name, error in result["errors"].items():
                print("[warn] watcher %s 出错: %s" % (name, error))
        print("mode=%s 发现=%d 新记录=%d" % (args.mode, result.get("considered", 0),
                                            len(result.get("stored") or ())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
