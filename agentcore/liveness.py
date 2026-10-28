"""Task liveness ledger — one durable row per long-running task.

Why this exists
---------------
Background execution already exists (terminal background jobs, durable queue,
external executors). What was missing is a *shared* answer to one question:

    Is this task still alive, and what is it doing?

A completion notification only answers that at the end. A queue ``status`` field
answers it only for queue jobs. This module owns the missing surface:

* **one row per task** — stable ``task_id``, owner, label, start time;
* **one heartbeat clock** — progress updates advance it, and a sweep turns
  silence into ``stalled`` instead of leaving a task "running" forever;
* **pid evidence** — a local child's disappearance becomes ``lost`` rather than
  a guessed success (boot id + process start time defeat pid reuse);
* **terminal write-once** — a late heartbeat cannot overwrite ``done/failed``.

It deliberately does **not** own scheduling, transport, or rendering. Runners
write here; a status tool reads here; a daemon can turn state transitions into
notifications. Keeping those apart is what lets the same ledger back terminal
jobs, durable jobs, and external executors without a second source of truth.

No third-party imports: the package import-boundary gate forbids them.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

#: Terminal states are write-once. ``stalled`` / ``lost`` are observations that a
#: later heartbeat may still clear (a runner can be restarted).
TERMINAL_STATES = frozenset({"done", "failed", "cancelled"})
KNOWN_STATES = frozenset(
    {"queued", "running", "stalled", "lost", "done", "failed", "cancelled"})
DEFAULT_STALL_SECONDS = 120.0
DEFAULT_DB_PATH = Path.home() / ".local" / "state" / "agentcore" / "liveness.db"

_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS task_liveness (
  task_id       TEXT PRIMARY KEY,
  kind          TEXT NOT NULL,
  owner         TEXT NOT NULL DEFAULT '',
  label         TEXT NOT NULL DEFAULT '',
  state         TEXT NOT NULL DEFAULT 'queued',
  started_at    REAL NOT NULL,
  heartbeat_at  REAL NOT NULL,
  updated_at    REAL NOT NULL,
  deadline      REAL,
  phase         TEXT NOT NULL DEFAULT '',
  progress_done INTEGER,
  progress_total INTEGER,
  detail        TEXT NOT NULL DEFAULT '',
  pid           INTEGER,
  pid_start     TEXT,
  host          TEXT NOT NULL DEFAULT '',
  exit_code     INTEGER,
  artifacts     TEXT NOT NULL DEFAULT '[]',
  tail          TEXT NOT NULL DEFAULT '',
  seq           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_liveness_state ON task_liveness(state);
CREATE INDEX IF NOT EXISTS idx_liveness_owner ON task_liveness(owner, updated_at);
CREATE INDEX IF NOT EXISTS idx_liveness_heartbeat ON task_liveness(heartbeat_at);
"""


def default_db_path() -> Path:
    """Ledger location: explicit env first, then the per-user state directory."""
    raw = os.environ.get("AGENTCORE_LIVENESS_DB", "").strip()
    if raw:
        return Path(raw).expanduser()
    state = os.environ.get("AGENTCORE_STATE_DIR", "").strip()
    if state:
        return Path(state).expanduser() / "liveness.db"
    return DEFAULT_DB_PATH


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def pid_start_time(pid: int) -> Optional[str]:
    """``boot_id:starttime`` for *pid*, or ``None`` when it cannot be read.

    ``starttime`` is field 22 of ``/proc/<pid>/stat``; the command name may contain
    spaces and parentheses, so split only after its last ``)``.
    """
    try:
        fields = Path("/proc/%d/stat" % int(pid)).read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        return _boot_id() + ":" + fields[19]
    except (OSError, IndexError, ValueError):
        return None


def is_pid_alive(pid: Optional[int], pid_start: Optional[str] = None) -> bool:
    """True only when *pid* is alive **and** (when given) still the same process.

    A pid without a recorded start time is accepted when it exists; callers that
    care about pid reuse should record the start time at registration (``register``
    does this automatically).
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    current = pid_start_time(pid)
    if current is None:
        return False
    return pid_start is None or current == pid_start


def _row_dict(row: sqlite3.Row) -> Dict[str, Any]:
    data = dict(row)
    try:
        data["artifacts"] = json.loads(data.get("artifacts") or "[]")
    except (TypeError, ValueError):
        data["artifacts"] = []
    return data


def _bounded_tail(text: Any, limit: int = 4000) -> str:
    value = "" if text is None else str(text)
    return value[-max(1, int(limit)):]


class LivenessLedger:
    """SQLite-backed task liveness store. One instance per database path."""

    def __init__(self, path: Optional[Any] = None, *, stall_seconds: float = DEFAULT_STALL_SECONDS,
                 clock: Optional[Callable[[], float]] = None):
        self.path = Path(path).expanduser() if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stall_seconds = float(stall_seconds)
        if self.stall_seconds <= 0:
            raise ValueError("stall_seconds must be positive")
        self._clock = clock or time.time
        self.ensure_schema()

    @contextlib.contextmanager
    def _schema_lock(self):
        """Serialize first-time DDL across the hook and tool plugins.

        Switching a brand-new file to WAL while another connection is also
        initializing can fail with ``database is locked`` and leave a 0-byte
        file. A sidecar flock makes the first writer authoritative; readers
        then see a committed table.
        """
        lock_path = str(self.path) + ".init.lock"
        try:
            import fcntl
        except ImportError:  # non-POSIX: SQLite busy_timeout remains the fallback
            yield
            return
        handle = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                os.close(handle)

    def ensure_schema(self, *, attempts: int = 5) -> None:
        """Create the table once, tolerating a concurrent first writer.

        Two plugins (a hook and a tool) may initialize the same file at the
        same instant. SQLite can expose an empty file before the other
        connection commits its DDL; retry rather than leaving a 0-byte ledger.
        """
        last = None
        with self._schema_lock():
            for attempt in range(max(1, int(attempts))):
                try:
                    with self.db() as db:
                        db.executescript(_SCHEMA)
                        row = db.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_liveness'"
                        ).fetchone()
                        if row is not None:
                            return
                except sqlite3.OperationalError as error:
                    last = error
                if attempt + 1 < attempts:
                    time.sleep(0.05 * (attempt + 1))
        raise sqlite3.OperationalError(
            "liveness schema unavailable: %s" % (last or "table missing"))

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _now(self) -> float:
        return float(self._clock())

    def register(self, task_id: str, *, kind: str, owner: str = "", label: str = "",
                 pid: Optional[int] = None, pid_start: Optional[str] = None,
                 host: str = "", deadline: Optional[float] = None,
                 phase: str = "", detail: str = "", state: str = "queued",
                 artifacts: Sequence[str] = (), started_at: Optional[float] = None) -> Dict[str, Any]:
        """Create or refresh a non-terminal task row.

        Re-registering an existing non-terminal task keeps its original
        ``started_at`` but refreshes owner/label/pid and the heartbeat. A terminal
        task is never reopened: the existing row is returned unchanged.
        """
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task_id must be a nonempty string")
        if not isinstance(kind, str) or not kind:
            raise ValueError("kind must be a nonempty string")
        if state not in ("queued", "running"):
            raise ValueError("register state must be queued or running")
        now = self._now()
        started = now if started_at is None else float(started_at)
        if pid is not None and pid_start is None:
            pid_start = pid_start_time(pid)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """INSERT INTO task_liveness
                   (task_id, kind, owner, label, state, started_at, heartbeat_at,
                    updated_at, deadline, phase, detail, pid, pid_start, host,
                    artifacts, seq)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                   ON CONFLICT(task_id) DO UPDATE SET
                     kind=excluded.kind, owner=excluded.owner, label=excluded.label,
                     heartbeat_at=excluded.heartbeat_at, updated_at=excluded.updated_at,
                     deadline=excluded.deadline, phase=excluded.phase,
                     detail=excluded.detail, pid=excluded.pid,
                     pid_start=excluded.pid_start, host=excluded.host,
                     artifacts=excluded.artifacts, seq=task_liveness.seq+1
                   WHERE task_liveness.state NOT IN ('done','failed','cancelled')""",
                (task_id, kind, owner or "", label or "", state, started, now, now,
                 deadline, phase or "", detail or "", pid, pid_start, host or "",
                 json.dumps(list(artifacts or ()))))
            row = db.execute("SELECT * FROM task_liveness WHERE task_id=?", (task_id,)).fetchone()
        return _row_dict(row) if row is not None else {"task_id": task_id, "state": state}

    def beat(self, task_id: str, *, phase: Optional[str] = None,
             detail: Optional[str] = None, progress: Optional[Tuple[int, int]] = None,
             pid: Optional[int] = None, pid_start: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Advance the heartbeat. A heartbeat also clears ``stalled`` / ``lost``.

        Returns the updated row, or ``None`` for an unknown or terminal task.
        """
        now = self._now()
        if pid is not None and pid_start is None:
            pid_start = pid_start_time(pid)
        sets = ["state='running'", "heartbeat_at=?", "updated_at=?", "seq=seq+1"]
        params: List[Any] = [now, now]
        if phase is not None:
            sets.append("phase=?")
            params.append(str(phase))
        if detail is not None:
            sets.append("detail=?")
            params.append(str(detail))
        if progress is not None:
            done, total = progress
            if not isinstance(done, int) or not isinstance(total, int) or done < 0 or total < 0:
                raise ValueError("progress must be (done,total) non-negative integers")
            sets.extend(["progress_done=?", "progress_total=?"])
            params.extend([done, total])
        if pid is not None:
            sets.extend(["pid=?", "pid_start=?"])
            params.extend([pid, pid_start])
        params.append(task_id)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE task_liveness SET " + ", ".join(sets) +
                " WHERE task_id=? AND state NOT IN ('done','failed','cancelled')",
                params)
            row = db.execute("SELECT * FROM task_liveness WHERE task_id=?", (task_id,)).fetchone()
        if row is None or row["state"] in TERMINAL_STATES:
            return None
        return _row_dict(row)

    def observe(self, task_id: str, *, heartbeat_at: Optional[float] = None,
                state: Optional[str] = None, detail: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Record an externally observed heartbeat without pretending it is now.

        A queue worker owns its own heartbeat; the status reader forwards the real
        timestamp here instead of calling ``beat`` (which would reset the clock and
        hide a stalled worker).
        """
        if state is not None and state not in KNOWN_STATES:
            raise ValueError("unknown liveness state")
        now = self._now()
        sets = ["updated_at=?", "seq=seq+1"]
        params: List[Any] = [now]
        if heartbeat_at is not None:
            sets.append("heartbeat_at=?")
            params.append(float(heartbeat_at))
        if state is not None:
            sets.append("state=?")
            params.append(state)
        if detail is not None:
            sets.append("detail=?")
            params.append(str(detail))
        params.append(task_id)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE task_liveness SET " + ", ".join(sets) +
                " WHERE task_id=? AND state NOT IN ('done','failed','cancelled')", params)
            row = db.execute("SELECT * FROM task_liveness WHERE task_id=?", (task_id,)).fetchone()
        if row is None or row["state"] in TERMINAL_STATES:
            return None
        return _row_dict(row)

    def finish(self, task_id: str, state: str = "done", *, exit_code: Optional[int] = None,
               detail: Optional[str] = None, artifacts: Optional[Sequence[str]] = None,
               tail: Optional[str] = None) -> Dict[str, Any]:
        """Write a terminal state exactly once. Later calls report ``changed=False``."""
        if state not in TERMINAL_STATES:
            raise ValueError("finish state must be one of " + ", ".join(sorted(TERMINAL_STATES)))
        now = self._now()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM task_liveness WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                db.rollback()
                return {"ok": False, "changed": False, "error": "unknown_task",
                        "task_id": task_id}
            if row["state"] in TERMINAL_STATES:
                db.rollback()
                return {"ok": True, "changed": False, "task": _row_dict(row)}
            sets = ["state=?", "updated_at=?", "heartbeat_at=?", "seq=seq+1"]
            params: List[Any] = [state, now, now]
            if exit_code is not None:
                sets.append("exit_code=?")
                params.append(int(exit_code))
            if detail is not None:
                sets.append("detail=?")
                params.append(str(detail))
            if artifacts is not None:
                sets.append("artifacts=?")
                params.append(json.dumps(list(artifacts)))
            if tail is not None:
                sets.append("tail=?")
                params.append(_bounded_tail(tail))
            params.append(task_id)
            db.execute("UPDATE task_liveness SET " + ", ".join(sets) + " WHERE task_id=?", params)
            row = db.execute("SELECT * FROM task_liveness WHERE task_id=?", (task_id,)).fetchone()
        return {"ok": True, "changed": True, "task": _row_dict(row)}

    def record_exit(self, task_id: str, exit_code: Optional[int], *,
                    tail: Optional[str] = None, detail: Optional[str] = None) -> Dict[str, Any]:
        """Convenience: exit 0 is done, any other observed exit is failed."""
        ok = exit_code == 0
        return self.finish(
            task_id, "done" if ok else "failed", exit_code=exit_code,
            detail=detail or ("exited 0" if ok else "exited %r" % (exit_code,)),
            tail=tail)

    def record_tail(self, task_id: str, text: Any, *, limit: int = 4000) -> bool:
        """Replace the bounded output tail without touching state or heartbeat."""
        with self.db() as db:
            cur = db.execute(
                "UPDATE task_liveness SET tail=?, updated_at=?, seq=seq+1 "
                "WHERE task_id=? AND state NOT IN ('done','failed','cancelled')",
                (_bounded_tail(text, limit), self._now(), task_id))
            return cur.rowcount == 1

    def get(self, task_id: str, *, require_owner: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self.db() as db:
            row = db.execute("SELECT * FROM task_liveness WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            return None
        data = _row_dict(row)
        if require_owner is not None and data.get("owner") != require_owner:
            return None
        return data

    def inspect(self, task_id: str, *, now: Optional[float] = None,
                pid_alive: Optional[Callable[[Optional[int], Optional[str]], bool]] = None,
                require_owner: Optional[str] = None) -> Dict[str, Any]:
        """Return one task with computed age/health fields. Never fabricates success."""
        row = self.get(task_id, require_owner=require_owner)
        if row is None:
            return {"ok": False, "error": "unknown_task", "task_id": task_id}
        at = self._now() if now is None else float(now)
        alive: Optional[bool] = None
        if row.get("pid"):
            checker = pid_alive or is_pid_alive
            alive = bool(checker(row.get("pid"), row.get("pid_start")))
        elapsed = max(0.0, at - float(row["started_at"]))
        heartbeat_age = max(0.0, at - float(row["heartbeat_at"]))
        progress = None
        if row.get("progress_done") is not None or row.get("progress_total") is not None:
            done = int(row.get("progress_done") or 0)
            total = int(row.get("progress_total") or 0)
            progress = {"done": done, "total": total,
                        "percent": round(done * 100.0 / total, 1) if total > 0 else None}
        state = row["state"]
        if state in TERMINAL_STATES:
            verdict = state
        elif alive is False:
            verdict = "lost"
        elif alive is True:
            verdict = "running"
        elif heartbeat_age > self.stall_seconds:
            verdict = "stalled"
        else:
            verdict = "running"
        row.update({
            "ok": True,
            "elapsed_s": round(elapsed, 1),
            "heartbeat_age_s": round(heartbeat_age, 1),
            "pid_alive": alive,
            "progress": progress,
            "verdict": verdict,
            "stalled": verdict == "stalled",
            "quiet": verdict == "running" and heartbeat_age > self.stall_seconds,
        })
        return row

    def list_tasks(self, *, owner: Optional[str] = None, active_only: bool = True,
                   limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        where: List[str] = []
        params: List[Any] = []
        if owner is not None:
            where.append("owner=?")
            params.append(owner)
        if active_only:
            where.append("state NOT IN ('done','failed','cancelled')")
        sql = "SELECT * FROM task_liveness"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self.db() as db:
            rows = db.execute(sql, params).fetchall()
        return [_row_dict(row) for row in rows]

    def sweep(self, *, now: Optional[float] = None,
              pid_alive: Optional[Callable[[Optional[int], Optional[str]], bool]] = None,
              stall_seconds: Optional[float] = None) -> List[Dict[str, Any]]:
        """Classify non-terminal tasks; return only rows whose state changed.

        * live pid → stays running (silence is reported, not treated as death);
        * recorded pid gone → ``lost``;
        * no pid and heartbeat older than the threshold → ``stalled``.
        """
        at = self._now() if now is None else float(now)
        threshold = self.stall_seconds if stall_seconds is None else float(stall_seconds)
        checker = pid_alive or is_pid_alive
        with self.db() as db:
            rows = db.execute(
                "SELECT * FROM task_liveness WHERE state NOT IN ('done','failed','cancelled')"
            ).fetchall()
            changed: List[Dict[str, Any]] = []
            for row in rows:
                old = row["state"]
                new = old
                detail = row["detail"]
                if row["pid"]:
                    if not checker(row["pid"], row["pid_start"]):
                        new = "lost"
                        detail = "process is gone"
                elif at - float(row["heartbeat_at"]) > threshold:
                    new = "stalled"
                    detail = "no heartbeat for %ds" % int(at - float(row["heartbeat_at"]))
                if new == old:
                    continue
                db.execute(
                    "UPDATE task_liveness SET state=?, updated_at=?, detail=?, seq=seq+1 "
                    "WHERE task_id=? AND state=?",
                    (new, at, detail, row["task_id"], old))
                out = _row_dict(row)
                out.update({"previous_state": old, "state": new, "detail": detail,
                            "updated_at": at})
                changed.append(out)
        return changed

    def counts(self) -> Dict[str, int]:
        with self.db() as db:
            rows = db.execute(
                "SELECT state, COUNT(*) AS n FROM task_liveness GROUP BY state").fetchall()
        return {row["state"]: int(row["n"]) for row in rows}


def open_ledger(path: Optional[Any] = None, **kwargs: Any) -> LivenessLedger:
    """Small factory used by plugins/tests; keeps construction in one place."""
    return LivenessLedger(path, **kwargs)


__all__ = [
    "LivenessLedger", "open_ledger", "default_db_path", "is_pid_alive",
    "pid_start_time", "TERMINAL_STATES", "KNOWN_STATES", "DEFAULT_STALL_SECONDS",
]
