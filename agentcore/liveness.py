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
#: ``unverified`` is a claim, not a finish: a runner said the work is done, but the
#: evidence the task registered at start does not agree yet. It stays updatable so a
#: later check can settle it — and it never counts as finished for a reader.
UNVERIFIED = "unverified"
KNOWN_STATES = frozenset(
    {"queued", "running", "stalled", "lost", UNVERIFIED, "done", "failed", "cancelled"})
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
  expects       TEXT NOT NULL DEFAULT '[]',
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
    for key in ("artifacts", "expects"):
        try:
            data[key] = json.loads(data.get(key) or "[]")
        except (TypeError, ValueError):
            data[key] = []
    return data


def _bounded_tail(text: Any, limit: int = 4000) -> str:
    value = "" if text is None else str(text)
    return value[-max(1, int(limit)):]


def _expectation_failures(row: Dict[str, Any], *, exit_code: Optional[int] = None) -> List[Dict[str, Any]]:
    """Which registered expectations this finished task does not satisfy.

    A runner saying "done" is one claim; the evidence the task registered when it
    started is another. When they disagree the task is not finished -- it is
    *unverified*, and a reader must see that instead of a false completion.
    """
    raw = row.get("expects")
    try:
        items = json.loads(raw) if isinstance(raw, str) else list(raw or ())
    except (TypeError, ValueError):
        return []
    failures: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        if kind == "file":
            path = Path(str(item.get("path") or "")).expanduser()
            minimum = int(item.get("min_bytes") or 1)
            try:
                size = path.stat().st_size if path.is_file() else -1
            except OSError:
                size = -1
            if size < minimum:
                failures.append({"kind": kind, "path": str(path),
                                 "reason": "文件不存在或小于 %d 字节" % minimum})
        elif kind == "exit0":
            code = exit_code if exit_code is not None else row.get("exit_code")
            if code != 0:
                failures.append({"kind": kind, "reason": "退出码 %r 不是 0" % (code,)})
        elif kind == "artifact":
            wanted = str(item.get("path") or "")
            if wanted and wanted not in (row.get("artifacts") or ()):
                failures.append({"kind": kind, "path": wanted, "reason": "产物未登记"})
        else:
            # An expectation nobody can evaluate is not satisfied by default.
            failures.append({"kind": kind, "reason": "未知验收类型 %r" % kind})
    return failures


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
                        # Older ledgers predate `expects`; add the column once.
                        columns = {row[1] for row in db.execute(
                            "PRAGMA table_info(task_liveness)").fetchall()}
                        if "expects" not in columns:
                            db.execute(
                                "ALTER TABLE task_liveness ADD COLUMN expects TEXT NOT NULL DEFAULT '[]'")
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
                 artifacts: Sequence[str] = (), started_at: Optional[float] = None,
                 expects: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
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
                    artifacts, expects, seq)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                   ON CONFLICT(task_id) DO UPDATE SET
                     kind=excluded.kind, owner=excluded.owner, label=excluded.label,
                     heartbeat_at=excluded.heartbeat_at, updated_at=excluded.updated_at,
                     deadline=excluded.deadline, phase=excluded.phase,
                     detail=excluded.detail, pid=excluded.pid,
                     pid_start=excluded.pid_start, host=excluded.host,
                     artifacts=excluded.artifacts,
                     expects=CASE WHEN excluded.expects='[]' THEN task_liveness.expects
                                  ELSE excluded.expects END,
                     seq=task_liveness.seq+1
                   WHERE task_liveness.state NOT IN ('done','failed','cancelled')""",
                (task_id, kind, owner or "", label or "", state, started, now, now,
                 deadline, phase or "", detail or "", pid, pid_start, host or "",
                 json.dumps(list(artifacts or ())),
                 json.dumps([dict(item) for item in (expects or ())], ensure_ascii=False)))
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
                " WHERE task_id=? AND state NOT IN ('done','failed','cancelled','unverified')",
                params)
            row = db.execute("SELECT * FROM task_liveness WHERE task_id=?", (task_id,)).fetchone()
        if row is None or row["state"] in TERMINAL_STATES or row["state"] == UNVERIFIED:
            # A task waiting on its own evidence accepts no heartbeat: it already
            # claimed the work is over, so "still running" would be a second lie.
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
                " WHERE task_id=? AND state NOT IN ('done','failed','cancelled','unverified')", params)
            row = db.execute("SELECT * FROM task_liveness WHERE task_id=?", (task_id,)).fetchone()
        if row is None or row["state"] in TERMINAL_STATES or row["state"] == UNVERIFIED:
            # A task waiting on its own evidence accepts no heartbeat: it already
            # claimed the work is over, so "still running" would be a second lie.
            return None
        return _row_dict(row)

    def finish(self, task_id: str, state: str = "done", *, exit_code: Optional[int] = None,
               detail: Optional[str] = None, artifacts: Optional[Sequence[str]] = None,
               tail: Optional[str] = None,
               require_owner: Optional[str] = None) -> Dict[str, Any]:
        """Write a terminal state exactly once. Later calls report ``changed=False``."""
        if state not in TERMINAL_STATES:
            raise ValueError("finish state must be one of " + ", ".join(sorted(TERMINAL_STATES)))
        now = self._now()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM task_liveness WHERE task_id=?", (task_id,)).fetchone()
            if row is None or (require_owner is not None and row["owner"] != require_owner):
                db.rollback()
                return {"ok": False, "changed": False, "error": "unknown_task",
                        "task_id": task_id}
            if row["state"] in TERMINAL_STATES:
                db.rollback()
                return {"ok": True, "changed": False, "task": _row_dict(row)}
            target = state
            verdict = {}
            if state == "done":
                # The gate: a claimed finish must also satisfy the evidence the task
                # registered at start, or it is only `unverified`.
                failures = _expectation_failures(_row_dict(row), exit_code=exit_code)
                if failures:
                    target = UNVERIFIED
                    verdict = {"unmet": failures}
                    summary = "；".join(str(item.get("reason")) for item in failures)[:300]
                    detail = (detail + " | " if detail else "") + "待验收：" + summary
            sets = ["state=?", "updated_at=?", "heartbeat_at=?", "seq=seq+1"]
            params: List[Any] = [target, now, now]
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
        result = {"ok": True, "changed": True, "task": _row_dict(row)}
        if target != state:
            # The caller asked for `done`; the evidence did not agree.
            result["gate"] = dict(verdict, requested=state, settled=target)
        return result

    def verify(self, task_id: str, *, exit_code: Optional[int] = None,
               require_owner: Optional[str] = None) -> Dict[str, Any]:
        """Re-check a task that claimed done but has not passed its own evidence.

        Returns ``changed=True`` only when every registered expectation now holds and
        the task settles as ``done``. A task that is still short keeps its
        ``unverified`` state and reports what is missing.
        """
        now = self._now()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM task_liveness WHERE task_id=?", (task_id,)).fetchone()
            if row is None or (require_owner is not None and row["owner"] != require_owner):
                db.rollback()
                return {"ok": False, "changed": False, "error": "unknown_task",
                        "task_id": task_id}
            if row["state"] in TERMINAL_STATES:
                db.rollback()
                return {"ok": True, "changed": False, "task": _row_dict(row)}
            if row["state"] != UNVERIFIED:
                db.rollback()
                return {"ok": False, "changed": False, "error": "not_awaiting_evidence",
                        "task": _row_dict(row)}
            failures = _expectation_failures(_row_dict(row), exit_code=exit_code)
            if failures:
                summary = "；".join(str(item.get("reason")) for item in failures)[:300]
                db.execute(
                    "UPDATE task_liveness SET detail=?, updated_at=?, seq=seq+1 WHERE task_id=?",
                    ("待验收：" + summary, now, task_id))
                row = db.execute("SELECT * FROM task_liveness WHERE task_id=?", (task_id,)).fetchone()
                db.commit()
                return {"ok": True, "changed": False, "unmet": failures,
                        "task": _row_dict(row)}
            sets = ["state='done'", "updated_at=?", "heartbeat_at=?", "seq=seq+1"]
            params: List[Any] = [now, now]
            if exit_code is not None:
                sets.append("exit_code=?")
                params.append(int(exit_code))
            params.append(task_id)
            db.execute("UPDATE task_liveness SET " + ", ".join(sets) + " WHERE task_id=?", params)
            row = db.execute("SELECT * FROM task_liveness WHERE task_id=?", (task_id,)).fetchone()
        return {"ok": True, "changed": True, "task": _row_dict(row)}

    def record_exit(self, task_id: str, exit_code: Optional[int], *,
                    tail: Optional[str] = None, detail: Optional[str] = None,
                    require_owner: Optional[str] = None) -> Dict[str, Any]:
        """Convenience: exit 0 is done, any other observed exit is failed."""
        ok = exit_code == 0
        return self.finish(
            task_id, "done" if ok else "failed", exit_code=exit_code,
            detail=detail or ("exited 0" if ok else "exited %r" % (exit_code,)),
            tail=tail, require_owner=require_owner)

    def record_tail(self, task_id: str, text: Any, *, limit: int = 4000) -> bool:
        """Replace the bounded output tail without touching state or heartbeat."""
        with self.db() as db:
            cur = db.execute(
                "UPDATE task_liveness SET tail=?, updated_at=?, seq=seq+1 "
                "WHERE task_id=? AND state NOT IN ('done','failed','cancelled','unverified')",
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
        note = None
        if verdict == "running" and alive is True and heartbeat_age > self.stall_seconds:
            note = ("进程仍可见，但最后心跳已是 %.0f 秒前；仅凭进程存在不能确认任务仍有进展。"
                    % heartbeat_age)
        elif verdict == "stalled":
            note = ("最后心跳已是 %.0f 秒前，且没有可核验的进程；任务结果尚未确认。"
                    % heartbeat_age)
        elif verdict == "lost":
            note = "无法确认原进程仍在；仅凭进程不可见不能判断任务最终结果。"
        row.update({
            "ok": True,
            "liveness_note": note,
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
            where.append("state NOT IN ('done','failed','cancelled','unverified')")
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
              stall_seconds: Optional[float] = None,
              owner: Optional[str] = None) -> List[Dict[str, Any]]:
        """Classify non-terminal tasks; return only rows whose state changed.

        * live pid → stays running (silence is reported, not treated as death);
        * recorded pid gone → ``lost``;
        * no pid and heartbeat older than the threshold → ``stalled``.
        """
        at = self._now() if now is None else float(now)
        threshold = self.stall_seconds if stall_seconds is None else float(stall_seconds)
        checker = pid_alive or is_pid_alive
        if owner is not None and (not isinstance(owner, str) or not owner):
            raise ValueError('owner must be a nonempty string')
        with self.db() as db:
            sql = ("SELECT * FROM task_liveness WHERE state NOT IN "
                   "('done','failed','cancelled','unverified')")
            params = ()
            if owner is not None:
                sql += " AND owner=?"
                params = (owner,)
            rows = db.execute(sql, params).fetchall()
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
                cursor = db.execute(
                    "UPDATE task_liveness SET state=?, updated_at=?, detail=?, seq=seq+1 "
                    "WHERE task_id=? AND state=? AND owner IS ?",
                    (new, at, detail, row["task_id"], old, row["owner"]))
                if cursor.rowcount != 1:
                    continue
                out = _row_dict(row)
                out.update({"previous_state": old, "state": new, "detail": detail,
                            "updated_at": at})
                changed.append(out)
        return changed

    def drain(self, timeout: float = 60.0, poll_interval: float = 1.0,
              owner: Optional[str] = None,
              sleep_fn: Optional[Callable[[float], None]] = None) -> Dict[str, Any]:
        """Wait until all non-terminal tasks reach a terminal state, or timeout is reached."""
        deadline = self._now() + max(0.0, float(timeout))
        sleeper = sleep_fn or getattr(self, "_sleep_fn", None) or time.sleep
        try:
            self.sweep_stale()
        except Exception:
            pass
        while True:
            active = [t for t in self.list_tasks(owner=owner, active_only=True)
                      if t.get("state") in ("queued", "running")]
            if not active:
                return {"ok": True, "drained": True, "active_count": 0, "active_tasks": []}
            if self._now() >= deadline:
                return {
                    "ok": False,
                    "drained": False,
                    "active_count": len(active),
                    "active_tasks": [t["task_id"] for t in active],
                    "timeout": timeout,
                }
            sleeper(max(0.05, float(poll_interval)))

    list = list_tasks

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


def main(argv=None) -> int:
    """Operator CLI: sweep stale rows or drain active tasks before gateway restart.

    ``python3 -m agentcore.liveness --db <ledger> [--drain] [--timeout 60] [--stall-seconds N] [--json]``
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Sweep or drain task liveness rows (pid evidence; never guesses success).")
    parser.add_argument("--db", default=None, help="ledger path (default: per-user state dir)")
    parser.add_argument("--stall-seconds", type=float, default=None,
                        help="silence threshold for a pid-less row (default: ledger default)")
    parser.add_argument("--drain", action="store_true",
                        help="drain active tasks until finished or timeout")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="drain timeout in seconds (default: 60)")
    parser.add_argument("--json", action="store_true", help="print the changed rows as JSON")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    ledger = LivenessLedger(args.db, **({} if args.stall_seconds is None
                                        else {"stall_seconds": args.stall_seconds}))
    if args.drain:
        res = ledger.drain(timeout=args.timeout)
        if args.json:
            print(json.dumps(res, ensure_ascii=False))
        else:
            if res["drained"]:
                print("drain complete: 0 active tasks in %s" % ledger.path)
            else:
                print("drain timed out after %.1fs: %d active task(s): %s"
                      % (args.timeout, res["active_count"], res["active_tasks"]))
        return 0 if res["drained"] else 1

    changed = ledger.sweep(stall_seconds=args.stall_seconds)
    if args.json:
        print(json.dumps({"db": str(ledger.path), "changed": changed,
                          "counts": ledger.counts()}, ensure_ascii=False))
    else:
        print("swept %d row(s) from %s; counts=%s"
              % (len(changed), ledger.path, json.dumps(ledger.counts(), sort_keys=True)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
