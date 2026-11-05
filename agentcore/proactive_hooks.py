"""Proactive hook engine — lightweight pre-condition probes that dispatch durable expert jobs.

Why this exists
---------------
``proactive_watch.py`` is strictly observational (``observe`` mode): it records what the
agent noticed and never acts. For scheduled domain operations (such as watching nightly
builds or polling external project trackers for new requirements), waking a full-context
LLM turn on every cron tick wastes tokens when nothing changed.

This module implements a two-stage proactive hook contract:

1. **Pre-Hook Probe (Zero-LLM Fast Path)**:
   Runs a bounded shell/script probe first. If the probe reports ``[SKIP]`` or
   ``{"trigger": false}``, the engine records a quiet check in ``HookLedger`` and exits
   immediately without invoking an LLM turn or sending any message.
2. **Idempotent Durable Dispatch (Active Path)**:
   When the probe detects actionable work (``{"trigger": true, "dedupe_key": "...", ...}``),
   the engine checks ``HookLedger`` for cooldown/idempotency on ``(hook_id, dedupe_key)``.
   New triggers are submitted directly into the target expert's ``Pool`` queue with a
   bound ``origin`` route so completion delivery automatically posts the human-readable
   receipt card when the expert finishes.

No third-party imports: the package import-boundary gate forbids them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

from agentcore.pool import Pool

DEFAULT_HOOK_DB = Path.home() / ".local" / "state" / "agentcore" / "proactive_hooks.db"
_HOOK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}\Z")
_TRIGGER_LINE_RE = re.compile(r"^\[TRIGGER(?::([^\]]+))?\]\s*(.*)$")

_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS hook_triggers (
  hook_id     TEXT NOT NULL,
  dedupe_key  TEXT NOT NULL,
  ts          REAL NOT NULL,
  task_id     TEXT NOT NULL,
  summary     TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (hook_id, dedupe_key)
);
CREATE TABLE IF NOT EXISTS hook_runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          REAL NOT NULL,
  hook_id     TEXT NOT NULL,
  status      TEXT NOT NULL,
  dedupe_key  TEXT NOT NULL DEFAULT '',
  task_id     TEXT NOT NULL DEFAULT '',
  detail      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_hook_runs_hook_ts ON hook_runs(hook_id, ts);
"""


def default_hook_db_path() -> Path:
    raw = os.environ.get("AGENTCORE_PROACTIVE_HOOKS_DB", "").strip()
    if raw:
        return Path(raw).expanduser()
    state = os.environ.get("AGENTCORE_STATE_DIR", "").strip()
    if state:
        return Path(state).expanduser() / "proactive_hooks.db"
    return DEFAULT_HOOK_DB


@dataclass(frozen=True)
class HookSpec:
    id: str
    role: str
    queue_root: str
    probe_cmd: Sequence[str]
    goal_template: str
    profile: str = "default"
    display_name: str = ""
    display_label: str = ""
    origin: Dict[str, str] = field(default_factory=dict)
    cooldown_seconds: float = 3600.0
    timeout_seconds: float = 60.0
    workdir: str = ""
    enabled: bool = True

    def validate(self) -> None:
        if not isinstance(self.id, str) or not _HOOK_ID_RE.fullmatch(self.id):
            raise ValueError(f"invalid hook id: {self.id!r}")
        if not isinstance(self.role, str) or not _ROLE_RE.fullmatch(self.role):
            raise ValueError(f"invalid hook role: {self.role!r}")
        if not isinstance(self.queue_root, str) or not Path(self.queue_root).is_absolute():
            raise ValueError("queue_root must be an absolute path")
        if (not isinstance(self.probe_cmd, (list, tuple)) or not self.probe_cmd
                or any(not isinstance(part, str) or not part for part in self.probe_cmd)):
            raise ValueError("probe_cmd must be a non-empty list of strings")
        if not isinstance(self.goal_template, str) or not 1 <= len(self.goal_template.strip()) <= 3500:
            raise ValueError("goal_template must be 1..3500 characters")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be non-negative")
        if not 1.0 <= self.timeout_seconds <= 600.0:
            raise ValueError("timeout_seconds must be between 1 and 600")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HookSpec":
        if not isinstance(data, dict):
            raise ValueError("hook spec must be a JSON object")
        spec = cls(
            id=str(data.get("id") or ""),
            role=str(data.get("role") or "dev"),
            queue_root=str(data.get("queue_root") or ""),
            probe_cmd=tuple(data.get("probe_cmd") or ()),
            goal_template=str(data.get("goal_template") or ""),
            profile=str(data.get("profile") or "default"),
            display_name=str(data.get("display_name") or data.get("role") or "dev"),
            display_label=str(data.get("display_label") or data.get("role") or "dev"),
            origin=dict(data.get("origin") or {}),
            cooldown_seconds=float(data.get("cooldown_seconds", 3600.0)),
            timeout_seconds=float(data.get("timeout_seconds", 60.0)),
            workdir=str(data.get("workdir") or ""),
            enabled=bool(data.get("enabled", True)),
        )
        spec.validate()
        return spec


def parse_probe_output(stdout: str, returncode: int = 0) -> Dict[str, Any]:
    """Parse structured JSON or line-prefixed probe output into a trigger decision."""
    text = (stdout or "").strip()
    if returncode != 0:
        return {
            "trigger": False,
            "error": f"probe_exit_{returncode}",
            "summary": text[:300],
            "details": text[:2000],
            "dedupe_key": "",
        }
    if not text or text == "[SKIP]" or text.startswith("[SKIP]"):
        return {"trigger": False, "summary": "", "details": "", "dedupe_key": ""}

    if text.startswith("{") and text.endswith("}"):
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            trigger = bool(payload.get("trigger", False))
            summary = str(payload.get("summary") or "").strip()
            details = str(payload.get("details") or summary).strip()
            raw_key = str(payload.get("dedupe_key") or "").strip()
            if trigger and not raw_key:
                raw_key = hashlib.sha256((summary + "\n" + details).encode("utf-8")).hexdigest()[:16]
            return {
                "trigger": trigger,
                "dedupe_key": raw_key,
                "summary": summary[:400],
                "details": details[:2500],
            }

    lines = text.splitlines()
    first = lines[0].strip()
    match = _TRIGGER_LINE_RE.match(first)
    if match:
        raw_key = (match.group(1) or "").strip()
        summary = (match.group(2) or "").strip()
        details = "\n".join(lines[1:]).strip() or summary
        if not raw_key:
            raw_key = hashlib.sha256((summary + "\n" + details).encode("utf-8")).hexdigest()[:16]
        return {
            "trigger": True,
            "dedupe_key": raw_key,
            "summary": (summary or details)[:400],
            "details": details[:2500],
        }

    # Unrecognized non-empty output defaults to trigger with content hash dedupe.
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return {
        "trigger": True,
        "dedupe_key": digest,
        "summary": first[:400],
        "details": text[:2500],
    }


def render_hook_goal(template: str, *, summary: str, details: str, dedupe_key: str) -> str:
    rendered = (
        template
        .replace("{{summary}}", summary)
        .replace("{{details}}", details)
        .replace("{{dedupe_key}}", dedupe_key)
    )
    if "{{summary}}" not in template and "{{details}}" not in template and details:
        rendered = rendered.rstrip() + "\n\n【探针发现】\n" + details
    return rendered[:4000]


class HookLedger:
    """Durable SQLite ledger for proactive hook cooldowns, idempotency, and audit history."""

    def __init__(self, path: Optional[Any] = None):
        self.path = Path(path) if path is not None else default_hook_db_path()

    def ensure_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(_SCHEMA)

    def _db(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def in_cooldown(self, hook_id: str, dedupe_key: str, *, cooldown_seconds: float,
                    now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        if not dedupe_key or cooldown_seconds <= 0:
            return None
        now = time.time() if now is None else now
        self.ensure_schema()
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM hook_triggers WHERE hook_id=? AND dedupe_key=?",
                (hook_id, dedupe_key),
            ).fetchone()
        if row is None:
            return None
        age = now - float(row["ts"])
        if age < cooldown_seconds:
            return dict(row, age_seconds=round(age, 1))
        return None

    def record_trigger(self, hook_id: str, dedupe_key: str, task_id: str, *,
                       summary: str = "", now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        self.ensure_schema()
        with self._db() as db:
            db.execute(
                "INSERT INTO hook_triggers(hook_id, dedupe_key, ts, task_id, summary)"
                " VALUES(?,?,?,?,?)"
                " ON CONFLICT(hook_id, dedupe_key) DO UPDATE SET"
                " ts=excluded.ts, task_id=excluded.task_id, summary=excluded.summary",
                (hook_id, dedupe_key, now, task_id, summary[:400]),
            )
            db.execute(
                "INSERT INTO hook_runs(ts, hook_id, status, dedupe_key, task_id, detail)"
                " VALUES(?,?,?,?,?,?)",
                (now, hook_id, "dispatched", dedupe_key, task_id, summary[:400]),
            )

    def record_run(self, hook_id: str, status: str, *, dedupe_key: str = "",
                   task_id: str = "", detail: str = "", now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        self.ensure_schema()
        with self._db() as db:
            db.execute(
                "INSERT INTO hook_runs(ts, hook_id, status, dedupe_key, task_id, detail)"
                " VALUES(?,?,?,?,?,?)",
                (now, hook_id, status, dedupe_key, task_id, detail[:400]),
            )

    def recent_runs(self, *, hook_id: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        self.ensure_schema()
        sql = "SELECT * FROM hook_runs"
        params: List[Any] = []
        if hook_id:
            sql += " WHERE hook_id=?"
            params.append(hook_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self._db() as db:
            return [dict(row) for row in db.execute(sql, params).fetchall()]


def evaluate_and_dispatch(
    spec: HookSpec,
    *,
    ledger: HookLedger,
    pool_factory: Callable[[str], Any] = Pool,
    liveness_ledger: Any = None,
    runner: Callable[..., Any] = subprocess.run,
    dry_run: bool = False,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Run one proactive hook's probe and, when triggered, submit a durable job."""
    spec.validate()
    now = time.time() if now is None else now
    if not spec.enabled:
        return {"hook_id": spec.id, "status": "disabled", "triggered": False}

    cwd = spec.workdir if (spec.workdir and Path(spec.workdir).is_dir()) else None
    try:
        proc = runner(
            list(spec.probe_cmd),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=spec.timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = f"{type(exc).__name__}: {exc}"
        ledger.record_run(spec.id, "probe_error", detail=detail, now=now)
        return {"hook_id": spec.id, "status": "probe_error", "triggered": False, "error": detail}

    parsed = parse_probe_output(getattr(proc, "stdout", "") or "", getattr(proc, "returncode", 1))
    if parsed.get("error"):
        err_detail = f"{parsed['error']}: {parsed.get('summary') or (getattr(proc, 'stderr', '') or '').strip()[:200]}"
        ledger.record_run(spec.id, "probe_failed", detail=err_detail, now=now)
        return {"hook_id": spec.id, "status": "probe_failed", "triggered": False, "error": err_detail}

    if not parsed.get("trigger"):
        ledger.record_run(spec.id, "skipped", now=now)
        return {"hook_id": spec.id, "status": "skipped", "triggered": False}

    dedupe_key = str(parsed["dedupe_key"])
    summary = str(parsed["summary"])
    details = str(parsed["details"])

    prior = ledger.in_cooldown(spec.id, dedupe_key, cooldown_seconds=spec.cooldown_seconds, now=now)
    if prior is not None:
        ledger.record_run(spec.id, "cooldown", dedupe_key=dedupe_key,
                          task_id=str(prior.get("task_id") or ""), detail=summary, now=now)
        return {
            "hook_id": spec.id,
            "status": "cooldown",
            "triggered": False,
            "dedupe_key": dedupe_key,
            "prior_task_id": prior.get("task_id"),
        }

    goal = render_hook_goal(spec.goal_template, summary=summary, details=details, dedupe_key=dedupe_key)
    if dry_run:
        return {
            "hook_id": spec.id,
            "status": "dry_run_triggered",
            "triggered": True,
            "dedupe_key": dedupe_key,
            "goal": goal,
        }

    session_tag = f"proactive-hook:{spec.id}"
    owner_hash = hashlib.sha256(json.dumps([spec.profile, session_tag]).encode("utf-8")).hexdigest()
    pool_dedupe = owner_hash + ":" + hashlib.sha256(f"{spec.id}:{dedupe_key}".encode("utf-8")).hexdigest()
    origin = dict(spec.origin)
    origin.setdefault("profile", spec.profile)
    origin.setdefault("session", session_tag)

    payload = {
        "goal": goal,
        "sources": [],
        "_dispatch_owner": owner_hash,
        "_colleague_role": spec.role,
        "_colleague_name": spec.display_name or spec.role,
        "_colleague_label": spec.display_label or spec.role,
        "_proactive_hook_id": spec.id,
        "_proactive_dedupe_key": dedupe_key,
    }
    pool = pool_factory(spec.queue_root)
    task_id = pool.submit(pool_dedupe, payload, replay_safe=True, max_tries=2, origin=origin)
    ledger.record_trigger(spec.id, dedupe_key, task_id, summary=summary, now=now)

    if liveness_ledger is not None:
        try:
            liveness_ledger.register(
                task_id,
                kind="dispatch",
                owner=f"{spec.profile}:{session_tag}",
                label=spec.display_label or spec.id,
                state="queued",
            )
        except Exception:
            pass

    return {
        "hook_id": spec.id,
        "status": "dispatched",
        "triggered": True,
        "dedupe_key": dedupe_key,
        "task_id": task_id,
        "summary": summary,
    }


def load_hook_specs(path: Path) -> List[HookSpec]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = data.get("hooks") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("hook config must contain a list of hooks")
    return [HookSpec.from_dict(item) for item in items]


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="proactive_hooks",
        description="Run lightweight proactive pre-hooks and dispatch durable expert tasks on trigger.",
    )
    parser.add_argument("--config", required=True, help="Path to JSON hook definitions file")
    parser.add_argument("--hook-id", default="", help="Only evaluate this specific hook ID")
    parser.add_argument("--db", default="", help="Path to HookLedger SQLite database")
    parser.add_argument("--liveness-db", default="", help="Optional LivenessLedger SQLite path")
    parser.add_argument("--dry-run", action="store_true", help="Run probes without submitting to Pool")
    args = parser.parse_args(list(argv) if argv is not None else None)

    specs = load_hook_specs(Path(args.config).expanduser())
    if args.hook_id:
        specs = [s for s in specs if s.id == args.hook_id]
        if not specs:
            raise SystemExit(f"unknown hook_id: {args.hook_id}")

    ledger = HookLedger(Path(args.db).expanduser() if args.db else None)
    liveness = None
    if args.liveness_db:
        try:
            from agentcore.liveness import LivenessLedger
            liveness = LivenessLedger(Path(args.liveness_db).expanduser())
        except Exception:
            liveness = None

    results = []
    for spec in specs:
        res = evaluate_and_dispatch(
            spec,
            ledger=ledger,
            liveness_ledger=liveness,
            dry_run=args.dry_run,
        )
        results.append(res)
    print(json.dumps({"ok": True, "results": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
