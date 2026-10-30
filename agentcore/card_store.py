"""Durable storage for task-card handles.

A card handle must survive a worker or gateway restart. A restarted process that can
re-read the handle keeps updating the *same* card instead of orphaning it and opening a
second one — that is the whole point of "one task, one message id".

This module is that store. It is deliberately independent of the job queue: it takes a
filesystem path, opens its own SQLite connection, and owns exactly one table. A host that
already runs a job database may point it at the same directory; keep the write paths
serialised the same way the job store does (SQLite WAL tolerates concurrent readers, but
writers on slow storage benefit from a local lock).

No third-party imports: the package import-boundary gate forbids them.
"""
from __future__ import annotations

import contextlib
import sqlite3
import time
from pathlib import Path
from typing import Callable, List, Optional

from .task_card import CardHandle, CardState, CardWriter

_SCHEMA = '''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS cards (
  task TEXT PRIMARY KEY,
  chat TEXT NOT NULL,
  message TEXT NOT NULL,
  sequence INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'queued',
  started REAL NOT NULL DEFAULT 0,
  updated REAL NOT NULL DEFAULT 0);
'''


class PersistentCardWriter(CardWriter):
    """A :class:`CardWriter` that persists the handle after every accepted frame.

    Dropped frames are not persisted: the stored sequence only ever advances when a frame
    was actually committed.
    """

    def __init__(self, store: "CardStore", handle: CardHandle, *,
                 clock: Optional[Callable[[], float]] = None):
        super().__init__(handle, clock=clock)
        self._store = store

    def commit(self, frame):
        accepted = super().commit(frame)
        if accepted is not None:
            self._store.save(self.handle)
        return accepted


class CardStore:
    """SQLite-backed ``task_id -> CardHandle`` mapping.

    ``path`` is the database file. Use a dedicated file (e.g. ``cards.db``) or share the
    job store's directory — this module never assumes a schema it does not own.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript(_SCHEMA)

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _handle(row) -> CardHandle:
        return CardHandle(
            task_id=row["task"],
            chat_id=row["chat"],
            message_id=row["message"],
            sequence=row["sequence"],
            state=CardState(row["state"]),
            started_at=row["started"],
        )

    def bind(self, task_id: str, chat_id: str, message_id: str) -> CardHandle:
        """Create the handle for a task, or return the existing one unchanged.

        Binding is idempotent: a restart that re-binds the same task must not reset its
        sequence or its state.
        """
        existing = self.load(task_id)
        if existing is not None:
            return existing
        handle = CardHandle(task_id=task_id, chat_id=chat_id, message_id=message_id)
        self.save(handle)
        return handle

    def load(self, task_id: str) -> Optional[CardHandle]:
        with self.db() as db:
            row = db.execute("SELECT * FROM cards WHERE task = ?", (task_id,)).fetchone()
        return self._handle(row) if row is not None else None

    def save(self, handle: CardHandle) -> None:
        with self.db() as db:
            db.execute(
                "INSERT INTO cards (task, chat, message, sequence, state, started, updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(task) DO UPDATE SET "
                "chat=excluded.chat, message=excluded.message, sequence=excluded.sequence, "
                "state=excluded.state, started=excluded.started, updated=excluded.updated",
                (handle.task_id, handle.chat_id, handle.message_id, handle.sequence,
                 handle.state.value, handle.started_at, time.time()))

    def writer(self, task_id: str) -> PersistentCardWriter:
        """Rebuild a writer for a task from persisted state.

        This is the restart-resume primitive: the returned writer continues the card's
        sequence where it left off, so late frames are still ordered against the frames
        the previous process already sent.
        """
        handle = self.load(task_id)
        if handle is None:
            raise KeyError("no card bound for task %r" % (task_id,))
        return PersistentCardWriter(self, handle)

    def active(self) -> List[CardHandle]:
        """Handles whose card has not reached a terminal state, oldest first."""
        with self.db() as db:
            rows = db.execute(
                "SELECT * FROM cards WHERE state NOT IN ('done', 'failed', 'cancelled') "
                "ORDER BY updated ASC").fetchall()
        return [self._handle(row) for row in rows]

    def forget(self, task_id: str) -> None:
        with self.db() as db:
            db.execute("DELETE FROM cards WHERE task = ?", (task_id,))
