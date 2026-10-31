"""Prefer-D3 durable Tab follow-up inbox (thin SQLite ledger).

Authority lives beside the pool root — not Temporal, not a second scheduler,
not Redis-as-authority. Process-local FollowUpQueue remains a cache; after
restart the ledger wins.

Schema (尺规 FINAL LOCK):
  required: id · task_id · text(≤2000) · kind · created_at(date-time) · state
  optional: owner(≤512) · origin · blocked_phase · ack_id
  kind:  follow_up | urgent_follow_up | replan
  state: queued | draining | applied | dropped | blocked_irreversible
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from agentcore.task_progress import IRREVERSIBLE_PHASES

KINDS = frozenset({"follow_up", "urgent_follow_up", "replan"})
STATES = frozenset({
    "queued", "draining", "applied", "dropped", "blocked_irreversible",
})
#: Restart peek surfaces only these live states (draining recovers to queued).
PEEK_STATES = frozenset({"queued", "blocked_irreversible"})
ORIGIN_KEYS = ("profile", "session", "route", "channel", "chat", "thread", "turn")
TEXT_MAX = 2000
OWNER_MAX = 512


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _clip_text(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("follow_up_text_required")
    return raw[:TEXT_MAX]


def _clip_owner(owner: str) -> str:
    return str(owner or "")[:OWNER_MAX]


def _origin_json(origin: Optional[dict]) -> Optional[str]:
    if not origin or not isinstance(origin, dict):
        return None
    slim = {k: origin[k] for k in ORIGIN_KEYS if k in origin}
    return json.dumps(slim, sort_keys=True) if slim else None


def _row_to_item(row: sqlite3.Row) -> dict:
    origin = None
    if row["origin"]:
        try:
            origin = json.loads(row["origin"])
        except (TypeError, ValueError, json.JSONDecodeError):
            origin = None
    item = {
        "id": row["id"],
        "task_id": row["task_id"],
        "text": row["text"],
        "kind": row["kind"],
        "created_at": row["created_at"],
        "state": row["state"],
        "owner": row["owner"] or "",
        "ack_id": row["ack_id"],
        "blocked_phase": row["blocked_phase"],
    }
    if origin is not None:
        item["origin"] = origin
    return item


class FollowUpStore:
    """SQLite follow-up ledger living next to a pool root (``follow_up.db``)."""

    def __init__(self, root):
        self._db_lock = threading.RLock()
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS follow_ups (
                  id TEXT PRIMARY KEY,
                  task_id TEXT NOT NULL,
                  text TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  state TEXT NOT NULL,
                  owner TEXT NOT NULL DEFAULT '',
                  origin TEXT,
                  blocked_phase TEXT,
                  ack_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_follow_ups_task_state
                  ON follow_ups(task_id, state);
                """
            )
        self.recover_after_restart()

    @contextlib.contextmanager
    def db(self):
        with self._db_lock:
            db = sqlite3.connect(str(self.root / "follow_up.db"), timeout=30)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()

    def recover_after_restart(self) -> int:
        """Crash mid-drain → surface as queued (ledger wins; never leave draining)."""
        with self.db() as db:
            cur = db.execute(
                "UPDATE follow_ups SET state='queued' WHERE state='draining'"
            )
            return int(cur.rowcount or 0)

    def enqueue(
        self,
        task_id: str,
        text: str,
        *,
        id: Optional[str] = None,  # noqa: A002 — schema field name
        kind: str = "follow_up",
        owner: str = "",
        origin: Optional[dict] = None,
        ack_id: Optional[str] = None,
        created_at: Optional[str] = None,
        state: str = "queued",
    ) -> dict:
        """Crash-safe insert. Idempotent on ``id`` (repeat returns the stored row)."""
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id_required")
        kind_s = str(kind or "follow_up")
        if kind_s not in KINDS:
            raise ValueError("invalid_follow_up_kind")
        state_s = str(state or "queued")
        if state_s not in STATES:
            raise ValueError("invalid_follow_up_state")
        item_id = str(id or uuid.uuid4().hex)
        if not item_id.strip():
            raise ValueError("id_required")
        body = _clip_text(text)
        owner_s = _clip_owner(owner)
        created = created_at or _utc_now_iso()
        origin_s = _origin_json(origin)
        ack_s = str(ack_id) if ack_id else None

        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM follow_ups WHERE id=?", (item_id,)
            ).fetchone()
            if existing is not None:
                return _row_to_item(existing)
            db.execute(
                "INSERT INTO follow_ups"
                "(id,task_id,text,kind,created_at,state,owner,origin,blocked_phase,ack_id)"
                " VALUES(?,?,?,?,?,?,?,?,NULL,?)",
                (
                    item_id, task_id.strip(), body, kind_s, created, state_s,
                    owner_s, origin_s, ack_s,
                ),
            )
            row = db.execute(
                "SELECT * FROM follow_ups WHERE id=?", (item_id,)
            ).fetchone()
        return _row_to_item(row)

    def peek(
        self,
        task_id: str,
        *,
        states: Optional[Sequence[str]] = None,
    ) -> list:
        """Ledger peek. Default: queued | blocked_irreversible (restart surface)."""
        key = str(task_id or "")
        wanted = tuple(states) if states is not None else tuple(sorted(PEEK_STATES))
        if not wanted:
            return []
        placeholders = ",".join("?" for _ in wanted)
        with self.db() as db:
            rows = db.execute(
                "SELECT * FROM follow_ups WHERE task_id=? AND state IN (%s)"
                " ORDER BY created_at ASC, id ASC" % placeholders,
                (key, *wanted),
            ).fetchall()
        return [_row_to_item(r) for r in rows]

    def queued_count(self, task_id: str) -> int:
        """Durable count of queued (+ blocked_irreversible) items for card peek."""
        return len(self.peek(task_id))

    def mark_state(
        self,
        item_id: str,
        state: str,
        *,
        blocked_phase: Optional[str] = None,
        ack_id: Optional[str] = None,
    ) -> Optional[dict]:
        if state not in STATES:
            raise ValueError("invalid_follow_up_state")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM follow_ups WHERE id=?", (item_id,)
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE follow_ups SET state=?, blocked_phase=COALESCE(?, blocked_phase),"
                " ack_id=COALESCE(?, ack_id) WHERE id=?",
                (state, blocked_phase, ack_id, item_id),
            )
            row = db.execute(
                "SELECT * FROM follow_ups WHERE id=?", (item_id,)
            ).fetchone()
        return _row_to_item(row)

    def drain(
        self,
        task_id: str,
        *,
        current_phase: str = "",
        allow_preempt: bool = False,
        mark_blocked: bool = True,
    ) -> list:
        """Pull live items for ONE subsequent turn.

        IRREVERSIBLE (no preempt): leave queued or mark ``blocked_irreversible``;
        never returns items (no second long exec / no preempt).
        """
        phase = str(current_phase or "").strip().lower()
        irreversible = (not allow_preempt) and phase in IRREVERSIBLE_PHASES
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT * FROM follow_ups WHERE task_id=? AND state IN ('queued','blocked_irreversible')"
                " ORDER BY created_at ASC, id ASC",
                (str(task_id or ""),),
            ).fetchall()
            if irreversible:
                if mark_blocked and phase:
                    for row in rows:
                        if row["state"] == "queued":
                            db.execute(
                                "UPDATE follow_ups SET state='blocked_irreversible',"
                                " blocked_phase=? WHERE id=?",
                                (phase, row["id"]),
                            )
                return []
            out = []
            for row in rows:
                db.execute(
                    "UPDATE follow_ups SET state='draining' WHERE id=?",
                    (row["id"],),
                )
                item = _row_to_item(row)
                item["state"] = "draining"
                out.append(item)
            return out

    def mark_applied(self, item_id: str) -> Optional[dict]:
        return self.mark_state(item_id, "applied")

    def mark_dropped(self, item_id: str) -> Optional[dict]:
        return self.mark_state(item_id, "dropped")

    def bind_ack(self, item_id: str, ack_id: str) -> Optional[dict]:
        """Attach ack_id without changing state (ok-ack linkage)."""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM follow_ups WHERE id=?", (item_id,)
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE follow_ups SET ack_id=? WHERE id=?",
                (str(ack_id), item_id),
            )
            row = db.execute(
                "SELECT * FROM follow_ups WHERE id=?", (item_id,)
            ).fetchone()
        return _row_to_item(row)
