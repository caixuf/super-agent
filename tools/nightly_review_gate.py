#!/usr/bin/env python3
"""Pre-run gate script for Hermes Cron nightly session review jobs.

Follows the official Hermes Cron script gate contract (cron/scheduler_prompt.py):
- Runs BEFORE the LLM is invoked.
- When the last non-empty stdout line is ``{"wakeAgent": false, ...}``, Hermes
  skips the LLM turn completely (0 tokens) and writes a silent audit record.
- When ``{"wakeAgent": true, ...}`` is emitted, the preceding stdout summary is
  prepended to the cron prompt as pre-collected session context.

Gates enforced:
1. Nightly window gate: only wakes the agent during local hours [start_hour, end_hour)
   (default 01:00..04:00), blocking daytime ``catch_up`` runs after gateway restarts.
2. Active background task gate: skips if any colleague or dev queue has a job
   currently in ``status='running'``.
3. Recent user session gate: queries ``$HERMES_HOME/state.db`` for non-cron
   interactive sessions active within the last N hours (default 24h) with >= 2
   messages; skips when there is nothing new to review.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import sys
import time


def _resolve_hermes_home(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env_home = os.environ.get("HERMES_HOME", "").strip()
    if env_home:
        return Path(env_home).expanduser().resolve()
    # Script is typically invoked from $HERMES_HOME/scripts/nightly_review_gate.py
    script_parent = Path(__file__).resolve().parent
    if script_parent.name == "scripts" and (script_parent.parent / "state.db").exists():
        return script_parent.parent
    return (Path.home() / ".hermes").resolve()


def _find_pool_dbs(hermes_home: Path, explicit_state_dir: str | None = None) -> list[Path]:
    candidates: list[Path] = []
    state_dirs: list[Path] = []
    if explicit_state_dir:
        state_dirs.append(Path(explicit_state_dir).expanduser().resolve())
    env_state = os.environ.get("SUPERAGENT_STATE_DIR", "").strip()
    if env_state:
        state_dirs.append(Path(env_state).expanduser().resolve())
    # Standard single-root layout: <user_home>/super_agent/<instance>/state
    for base in (hermes_home.parent.parent, hermes_home.parent, Path.home()):
        sa_root = base / "super_agent"
        if sa_root.is_dir():
            for st in sorted(sa_root.glob("*/state")):
                if st.is_dir() and st not in state_dirs:
                    state_dirs.append(st)
    seen: set[Path] = set()
    for st in state_dirs:
        for pattern in ("colleague-queues/*/pool.db", "dev-queue/pool.db", "analysis-queue/pool.db"):
            for db_path in sorted(st.glob(pattern)):
                resolved = db_path.resolve()
                if resolved.is_file() and resolved not in seen:
                    seen.add(resolved)
                    candidates.append(resolved)
    return candidates


def count_running_pool_jobs(pool_dbs: list[Path]) -> int:
    running = 0
    for db_path in pool_dbs:
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0) as conn:
                row = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()
                if row and row[0]:
                    running += int(row[0])
        except Exception:
            continue
    return running


def fetch_recent_interactive_sessions(
    state_db: Path,
    *,
    now: float,
    lookback_hours: float = 24.0,
    min_messages: int = 2,
    limit: int = 10,
) -> list[dict]:
    if not state_db.is_file():
        return []
    cutoff = now - (lookback_hours * 3600.0)
    try:
        with sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=5.0) as conn:
            conn.row_factory = sqlite3.Row
            cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
            activity_expr = "COALESCE(last_activity_at, started_at)" if "last_activity_at" in cols else "started_at"
            rows = conn.execute(
                f"""
                SELECT id, source, started_at, {activity_expr} AS active_at,
                       COALESCE(message_count, 0) AS message_count,
                       COALESCE(tool_call_count, 0) AS tool_call_count,
                       COALESCE(title, '') AS title
                FROM sessions
                WHERE {activity_expr} >= ?
                  AND LOWER(COALESCE(source, '')) != 'cron'
                  AND id NOT LIKE 'cron_%'
                  AND COALESCE(message_count, 0) >= ?
                ORDER BY active_at DESC
                LIMIT ?
                """,
                (cutoff, int(min_messages), int(limit)),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def evaluate_gate(
    *,
    hermes_home: Path,
    state_dir: str | None = None,
    now_dt: datetime | None = None,
    start_hour: int = 1,
    end_hour: int = 4,
    lookback_hours: float = 24.0,
    min_messages: int = 2,
    force: bool = False,
) -> tuple[str, dict]:
    now_dt = now_dt or datetime.now().astimezone()
    now_ts = now_dt.timestamp()
    hour = now_dt.hour

    if not force:
        in_window = (
            (start_hour <= hour < end_hour)
            if start_hour <= end_hour
            else (hour >= start_hour or hour < end_hour)
        )
        if not in_window:
            return "", {
                "wakeAgent": False,
                "reason": "outside_nightly_window",
                "hour": hour,
                "allowed_window": [start_hour, end_hour],
            }

    pool_dbs = _find_pool_dbs(hermes_home, state_dir)
    running_jobs = count_running_pool_jobs(pool_dbs)
    if running_jobs > 0 and not force:
        return "", {
            "wakeAgent": False,
            "reason": "active_tasks_running",
            "running_jobs": running_jobs,
        }

    state_db = hermes_home / "state.db"
    sessions = fetch_recent_interactive_sessions(
        state_db,
        now=now_ts,
        lookback_hours=lookback_hours,
        min_messages=min_messages,
    )
    if not sessions:
        return "", {
            "wakeAgent": False,
            "reason": "no_recent_sessions",
            "lookback_hours": lookback_hours,
        }

    lines = [
        f"Pre-fetched {len(sessions)} interactive session(s) active in the last {lookback_hours:g}h:"
    ]
    for s in sessions:
        sid = s.get("id", "")
        src = s.get("source", "")
        msgs = s.get("message_count", 0)
        tools = s.get("tool_call_count", 0)
        title = (s.get("title") or "").strip().replace("\n", " ")[:80]
        lines.append(f"- session_id={sid} source={src} messages={msgs} tool_calls={tools} title={title!r}")

    return "\n".join(lines), {
        "wakeAgent": True,
        "reason": "ready",
        "session_count": len(sessions),
        "session_ids": [s.get("id", "") for s in sessions],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", default=None, help="Path to profile HERMES_HOME")
    parser.add_argument("--state-dir", default=None, help="Path to super_agent state directory")
    parser.add_argument(
        "--start-hour",
        type=int,
        default=int(os.environ.get("NIGHTLY_WINDOW_START_HOUR", "1")),
    )
    parser.add_argument(
        "--end-hour",
        type=int,
        default=int(os.environ.get("NIGHTLY_WINDOW_END_HOUR", "4")),
    )
    parser.add_argument(
        "--lookback-hours",
        type=float,
        default=float(os.environ.get("NIGHTLY_LOOKBACK_HOURS", "24")),
    )
    parser.add_argument(
        "--min-messages",
        type=int,
        default=int(os.environ.get("NIGHTLY_MIN_MESSAGES", "2")),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=os.environ.get("NIGHTLY_GATE_FORCE", "") == "1",
        help="Bypass time-window and running-job gates (still checks recent sessions)",
    )
    args = parser.parse_args(argv)

    hermes_home = _resolve_hermes_home(args.hermes_home)
    summary, payload = evaluate_gate(
        hermes_home=hermes_home,
        state_dir=args.state_dir,
        start_hour=args.start_hour,
        end_hour=args.end_hour,
        lookback_hours=args.lookback_hours,
        min_messages=args.min_messages,
        force=args.force,
    )
    if summary:
        print(summary)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

