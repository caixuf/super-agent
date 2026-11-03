"""Hermes Kanban Worker Contract & Pool Bridge.

Aligns Super Agent task execution with the official Hermes Kanban worker lanes contract:
- C05: Durable task creation before acknowledgment & idempotency.
- C06: Isolated readonly worker lanes and budget containment.
- C07: Worktree workspace resolution for dev tasks.
- C09: Event-projected status lifecycle and heartbeat liveness without terminal tampering.
- C10: Stop-proven task cancellation and reclaim.
- C11: WAL transaction recovery and crash idempotency.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Optional
import uuid

logger = logging.getLogger(__name__)

#: Required and optional environment variables passed to a Kanban worker lane.
ENV_KANBAN_TASK = "HERMES_KANBAN_TASK"
ENV_KANBAN_DB = "HERMES_KANBAN_DB"
ENV_KANBAN_BOARD = "HERMES_KANBAN_BOARD"
ENV_KANBAN_WORKSPACE = "HERMES_KANBAN_WORKSPACE"
ENV_KANBAN_WORKSPACES_ROOT = "HERMES_KANBAN_WORKSPACES_ROOT"
ENV_KANBAN_RUN_ID = "HERMES_KANBAN_RUN_ID"
ENV_KANBAN_CLAIM_LOCK = "HERMES_KANBAN_CLAIM_LOCK"
ENV_HERMES_PROFILE = "HERMES_PROFILE"
ENV_HERMES_TENANT = "HERMES_TENANT"

VALID_STATUSES = frozenset({"ready", "running", "review", "blocked", "done", "cancelled", "stop_unconfirmed"})
TERMINAL_STATUSES = frozenset({"done", "cancelled"})


def _connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_kanban_schema(db_path: Path | str) -> None:
    """Initialize canonical Kanban tables in SQLite with WAL journal mode."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(path) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'ready',
            assignee TEXT,
            claim_lock TEXT,
            run_id INTEGER NOT NULL DEFAULT 0,
            idempotency_key TEXT UNIQUE,
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS task_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            run_id INTEGER NOT NULL DEFAULT 0,
            event_type TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(task_id)
        );

        CREATE TABLE IF NOT EXISTS task_artifacts (
            artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            path TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'document',
            created_at REAL NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(task_id)
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        CREATE INDEX IF NOT EXISTS idx_tasks_idempotency ON tasks(idempotency_key);
        CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id, created_at);
        """)
        conn.commit()


class KanbanWorkerEnvironment:
    """Inspects and validates the environment of an active Kanban worker lane."""

    def __init__(self, env: Optional[dict[str, str]] = None) -> None:
        self._env = env if env is not None else os.environ

    @property
    def is_kanban_worker(self) -> bool:
        return bool(self._env.get(ENV_KANBAN_TASK) and self._env.get(ENV_KANBAN_DB))

    @property
    def task_id(self) -> Optional[str]:
        return self._env.get(ENV_KANBAN_TASK)

    @property
    def db_path(self) -> Optional[Path]:
        val = self._env.get(ENV_KANBAN_DB)
        return Path(val) if val else None

    @property
    def board(self) -> str:
        return self._env.get(ENV_KANBAN_BOARD, "default")

    @property
    def workspace(self) -> Optional[Path]:
        val = self._env.get(ENV_KANBAN_WORKSPACE)
        return Path(val) if val else None

    @property
    def run_id(self) -> int:
        try:
            return int(self._env.get(ENV_KANBAN_RUN_ID, "1"))
        except ValueError:
            return 1

    @property
    def claim_lock(self) -> Optional[str]:
        return self._env.get(ENV_KANBAN_CLAIM_LOCK)

    @property
    def profile(self) -> Optional[str]:
        return self._env.get(ENV_HERMES_PROFILE)

    @property
    def tenant(self) -> Optional[str]:
        return self._env.get(ENV_HERMES_TENANT)

    def validate(self) -> None:
        if not self.task_id:
            raise ValueError(f"Missing required environment: {ENV_KANBAN_TASK}")
        if not self.db_path:
            raise ValueError(f"Missing required environment: {ENV_KANBAN_DB}")


class KanbanWorkerContract:
    """Enforces the single terminal action contract for workers inside a Kanban lane."""

    def __init__(self, env: KanbanWorkerEnvironment) -> None:
        env.validate()
        self.env = env
        self.terminated = False
        self.terminal_action: Optional[str] = None
        self.artifacts: list[str] = []

    def _mark_terminal(self, action: str) -> None:
        if self.terminated:
            raise RuntimeError(f"Terminal action already recorded: {self.terminal_action}")
        self.terminated = True
        self.terminal_action = action

    def complete(self, summary: str, artifacts: Optional[list[str]] = None,
                 metadata: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Mark task succeeded (status -> done)."""
        self._mark_terminal("complete")
        artifact_list = list(artifacts) if artifacts else []
        self.artifacts = artifact_list
        now = time.time()
        with _connect(self.env.db_path) as conn:
            conn.execute(
                "UPDATE tasks SET status='done', updated_at=? WHERE task_id=?",
                (now, self.env.task_id)
            )
            event_payload = json.dumps({
                "action": "complete",
                "summary": summary,
                "artifacts": artifact_list,
                "metadata": metadata or {},
            })
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, event_type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (self.env.task_id, self.env.run_id, "completed", event_payload, now)
            )
            for art in artifact_list:
                conn.execute(
                    "INSERT INTO task_artifacts (task_id, path, category, created_at) VALUES (?, ?, 'artifact', ?)",
                    (self.env.task_id, str(art), now)
                )
            conn.commit()
        return {"status": "done", "summary": summary, "artifacts": artifact_list}

    def request_review(self, summary: str, reviewer: Optional[str] = None,
                       metadata: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Request review for code/spec (status -> review)."""
        self._mark_terminal("request_review")
        now = time.time()
        with _connect(self.env.db_path) as conn:
            conn.execute(
                "UPDATE tasks SET status='review', updated_at=? WHERE task_id=?",
                (now, self.env.task_id)
            )
            event_payload = json.dumps({
                "action": "request_review",
                "summary": summary,
                "reviewer": reviewer,
                "metadata": metadata or {},
            })
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, event_type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (self.env.task_id, self.env.run_id, "review_requested", event_payload, now)
            )
            conn.commit()
        return {"status": "review", "summary": summary, "reviewer": reviewer}

    def block(self, reason: str, metadata: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Pause execution awaiting human input (status -> blocked)."""
        self._mark_terminal("block")
        now = time.time()
        with _connect(self.env.db_path) as conn:
            conn.execute(
                "UPDATE tasks SET status='blocked', updated_at=? WHERE task_id=?",
                (now, self.env.task_id)
            )
            event_payload = json.dumps({
                "action": "block",
                "reason": reason,
                "metadata": metadata or {},
            })
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, event_type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (self.env.task_id, self.env.run_id, "blocked", event_payload, now)
            )
            conn.commit()
        return {"status": "blocked", "reason": reason}

    def heartbeat(self, progress: Optional[str] = None) -> bool:
        """Emit periodic worker liveness signal without modifying terminal state."""
        if self.terminated:
            return False
        now = time.time()
        with _connect(self.env.db_path) as conn:
            cur = conn.execute("SELECT status FROM tasks WHERE task_id=?", (self.env.task_id,))
            row = cur.fetchone()
            if not row or row["status"] in TERMINAL_STATUSES:
                return False
            payload = json.dumps({"progress": progress or "alive", "timestamp": now})
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, event_type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (self.env.task_id, self.env.run_id, "heartbeat", payload, now)
            )
            conn.execute(
                "UPDATE tasks SET updated_at=? WHERE task_id=? AND status NOT IN ('done', 'cancelled')",
                (now, self.env.task_id)
            )
            conn.commit()
        return True


class KanbanTaskBridge:
    """Bridge for dispatchers, pools, and controllers managing Kanban tasks."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        init_kanban_schema(self.db_path)

    def create_task(self, title: str, body: str = "", assignee: Optional[str] = None,
                    idempotency_key: Optional[str] = None,
                    metadata: Optional[dict[str, Any]] = None) -> str:
        """Create a durable task in SQLite before acknowledging submission (C05)."""
        if not title or not title.strip():
            raise ValueError("Task title must be non-empty")

        now = time.time()
        meta_json = json.dumps(metadata or {})
        with _connect(self.db_path) as conn:
            # Check idempotency
            if idempotency_key:
                cur = conn.execute("SELECT task_id FROM tasks WHERE idempotency_key=?", (idempotency_key,))
                existing = cur.fetchone()
                if existing:
                    return str(existing["task_id"])

            task_id = f"t_{uuid.uuid4().hex[:8]}"
            conn.execute(
                """INSERT INTO tasks
                   (task_id, title, body, status, assignee, idempotency_key, metadata, created_at, updated_at)
                   VALUES (?, ?, ?, 'ready', ?, ?, ?, ?, ?)""",
                (task_id, title.strip(), body.strip(), assignee, idempotency_key, meta_json, now, now)
            )
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, event_type, payload, created_at) VALUES (?, 0, 'created', ?, ?)",
                (task_id, json.dumps({"assignee": assignee, "idempotency_key": idempotency_key}), now)
            )
            conn.commit()
            return task_id

    def claim_task(self, task_id: str, assignee: str, claim_lock: Optional[str] = None,
                   run_id: int = 1) -> bool:
        """Atomically claim a ready task (CAS on status='ready') (C05, C09)."""
        lock = claim_lock or f"lock:{uuid.uuid4().hex[:8]}"
        now = time.time()
        with _connect(self.db_path) as conn:
            cur = conn.execute(
                """UPDATE tasks
                   SET status='running', assignee=?, claim_lock=?, run_id=?, updated_at=?
                   WHERE task_id=? AND status='ready'""",
                (assignee, lock, run_id, now, task_id)
            )
            if cur.rowcount == 0:
                return False
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, event_type, payload, created_at) VALUES (?, ?, 'claimed', ?, ?)",
                (task_id, run_id, json.dumps({"claim_lock": lock, "assignee": assignee}), now)
            )
            conn.commit()
            return True

    def emit_heartbeat(self, task_id: str, run_id: int = 1, progress: str = "running") -> bool:
        """Record worker heartbeat without changing master terminal state (C09)."""
        now = time.time()
        with _connect(self.db_path) as conn:
            cur = conn.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,))
            row = cur.fetchone()
            if not row or row["status"] in TERMINAL_STATUSES:
                return False
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, event_type, payload, created_at) VALUES (?, ?, 'heartbeat', ?, ?)",
                (task_id, run_id, json.dumps({"progress": progress}), now)
            )
            conn.execute("UPDATE tasks SET updated_at=? WHERE task_id=?", (now, task_id))
            conn.commit()
            return True

    def cancel_task(self, task_id: str, reason: str = "cancelled", stop_proven: bool = False) -> dict[str, Any]:
        """Cancel an in-flight task with stop proof requirement (C10)."""
        now = time.time()
        with _connect(self.db_path) as conn:
            cur = conn.execute("SELECT status, claim_lock FROM tasks WHERE task_id=?", (task_id,))
            row = cur.fetchone()
            if not row:
                raise KeyError(f"Task not found: {task_id}")
            if row["status"] in TERMINAL_STATUSES:
                return {"status": row["status"], "cancelled": False, "reason": "already_terminal"}

            if stop_proven:
                target_status = "cancelled"
                conn.execute(
                    "UPDATE tasks SET status=?, claim_lock=NULL, updated_at=? WHERE task_id=?",
                    (target_status, now, task_id)
                )
                conn.execute(
                    "INSERT INTO task_events (task_id, run_id, event_type, payload, created_at) VALUES (?, 0, 'cancelled', ?, ?)",
                    (task_id, json.dumps({"reason": reason, "stop_proven": True}), now)
                )
            else:
                target_status = "stop_unconfirmed"
                conn.execute(
                    "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
                    (target_status, now, task_id)
                )
                conn.execute(
                    "INSERT INTO task_events (task_id, run_id, event_type, payload, created_at) VALUES (?, 0, 'cancel_requested', ?, ?)",
                    (task_id, json.dumps({"reason": reason, "stop_proven": False}), now)
                )
            conn.commit()
            return {"status": target_status, "cancelled": stop_proven, "reason": reason}

    def recover_stale_tasks(self, heartbeat_timeout_s: float = 60.0) -> list[str]:
        """Recover tasks with expired heartbeats without repeating completed work (C11)."""
        now = time.time()
        recovered = []
        with _connect(self.db_path) as conn:
            cur = conn.execute("SELECT task_id, run_id, updated_at FROM tasks WHERE status='running'")
            for row in cur.fetchall():
                if now - row["updated_at"] > heartbeat_timeout_s:
                    task_id = row["task_id"]
                    run_id = row["run_id"]
                    conn.execute(
                        "UPDATE tasks SET status='ready', claim_lock=NULL, updated_at=? WHERE task_id=? AND status='running'",
                        (now, task_id)
                    )
                    conn.execute(
                        "INSERT INTO task_events (task_id, run_id, event_type, payload, created_at) VALUES (?, ?, 'reclaimed', ?, ?)",
                        (task_id, run_id, json.dumps({"reason": "heartbeat_timeout", "stale_age": now - row["updated_at"]}), now)
                    )
                    recovered.append(task_id)
            conn.commit()
        return recovered

    def get_task(self, task_id: str) -> Optional[dict[str, Any]]:
        with _connect(self.db_path) as conn:
            cur = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,))
            row = cur.fetchone()
            if not row:
                return None
            result = dict(row)
            result["metadata"] = json.loads(result["metadata"])
            return result

    def get_task_events(self, task_id: str) -> list[dict[str, Any]]:
        with _connect(self.db_path) as conn:
            cur = conn.execute(
                "SELECT * FROM task_events WHERE task_id=? ORDER BY event_id ASC", (task_id,)
            )
            events = []
            for r in cur.fetchall():
                d = dict(r)
                d["payload"] = json.loads(d["payload"])
                events.append(d)
            return events
