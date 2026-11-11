from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import pytest

from tools.room_query import get_recent_events, list_rooms


def test_room_query_empty_or_nonexistent(tmp_path: Path):
    non_existent = tmp_path / "non_existent.db"
    assert list_rooms(non_existent) == []
    assert get_recent_events(non_existent) == []


def test_room_query_with_db(tmp_path: Path):
    db_path = tmp_path / "shared-state.db"
    conn = sqlite3.connect(str(db_path))
    c = conn.cursor()
    c.execute("""
        CREATE TABLE hosted_rooms (
            room_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE hosted_room_events (
            seq INTEGER PRIMARY KEY,
            room_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            actor_json TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    c.execute(
        "INSERT INTO hosted_rooms (room_id, name, created_at) VALUES (?, ?, ?)",
        ("r-1", "test-room", "2026-11-11T12:00:00Z"),
    )
    c.execute(
        """
        INSERT INTO hosted_room_events (seq, room_id, kind, actor_json, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            "r-1",
            "message.member",
            json.dumps({"id": "architect"}),
            json.dumps({"text": "architecture plan ready"}),
            "2026-11-11T12:01:00Z",
        ),
    )
    conn.commit()
    conn.close()

    rooms = list_rooms(db_path)
    assert len(rooms) == 1
    assert rooms[0]["room_id"] == "r-1"
    assert rooms[0]["name"] == "test-room"

    events = get_recent_events(db_path, "r-1")
    assert len(events) == 1
    assert events[0]["actor"] == "architect"
    assert events[0]["text"] == "architecture plan ready"
