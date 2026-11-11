#!/usr/bin/env python3
"""CLI utility to query Hermes Hosted Rooms (multi-agent discussion groups).

Reads shared room states from ~/.hermes/shared-state.db.
Usage:
  room_query list
  room_query recent [--room <name_or_id>] [--limit 5]
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Dict, List, Optional


def get_shared_state_db(path: Optional[str] = None) -> Path:
    if path:
        return Path(path).expanduser().resolve()
    return Path.home() / ".hermes" / "shared-state.db"


def list_rooms(db_path: Path) -> List[Dict[str, Any]]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    try:
        rows = c.execute(
            "SELECT room_id, name, created_at FROM hosted_rooms ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def get_recent_events(
    db_path: Path,
    room_id_or_name: Optional[str] = None,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    try:
        target_room_id = None
        if room_id_or_name:
            r = c.execute(
                "SELECT room_id FROM hosted_rooms WHERE room_id = ? OR name = ?",
                (room_id_or_name, room_id_or_name),
            ).fetchone()
            if r:
                target_room_id = r["room_id"]
        else:
            r = c.execute(
                "SELECT room_id FROM hosted_rooms ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if r:
                target_room_id = r["room_id"]

        if not target_room_id:
            return []

        rows = c.execute(
            """
            SELECT seq, kind, actor_json, payload_json, created_at
            FROM hosted_room_events
            WHERE room_id = ? AND kind IN ('message.member', 'message.user')
            ORDER BY seq DESC
            LIMIT ?
            """,
            (target_room_id, limit),
        ).fetchall()

        events = []
        for row in reversed(rows):
            actor = {}
            payload = {}
            try:
                actor = json.loads(row["actor_json"])
            except Exception:
                pass
            try:
                payload = json.loads(row["payload_json"])
            except Exception:
                pass
            events.append({
                "seq": row["seq"],
                "kind": row["kind"],
                "actor": actor.get("id") or actor.get("profile") or actor.get("kind", "unknown"),
                "text": payload.get("text", ""),
                "created_at": row["created_at"],
            })
        return events
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=str, default="", help="Path to shared-state.db")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="List hosted rooms")

    p_recent = sub.add_parser("recent", help="Get recent messages in a room")
    p_recent.add_argument("--room", type=str, default="", help="Room ID or Name")
    p_recent.add_argument("--limit", type=int, default=5, help="Number of messages")

    args = parser.parse_args()
    db = get_shared_state_db(args.db)

    if args.command == "list":
        rooms = list_rooms(db)
        if args.json:
            print(json.dumps({"ok": True, "rooms": rooms}, ensure_ascii=False, indent=2))
        else:
            if not rooms:
                print("No hosted rooms found.")
            else:
                print(f"=== Hosted Rooms ({len(rooms)} total) ===")
                for r in rooms:
                    print(f"- [{r['room_id']}] {r['name']}")
        return 0

    if args.command == "recent" or not args.command:
        room_arg = getattr(args, "room", "")
        limit_arg = getattr(args, "limit", 5)
        events = get_recent_events(db, room_arg, limit_arg)
        if args.json:
            print(json.dumps({"ok": True, "events": events}, ensure_ascii=False, indent=2))
        else:
            if not events:
                print("No recent messages found.")
            else:
                print(f"=== Recent Discussion Events (Latest {len(events)}) ===")
                for e in events:
                    print(f"[{e['actor']}]:\n{e['text']}\n" + "-" * 40)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
