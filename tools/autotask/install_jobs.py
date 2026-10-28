"""Install autotask job files into a live Hermes cron store (idempotent, additive).

    python3 tools/autotask/install_jobs.py --cron-file <profile>/cron/jobs.json \
        --profile-path /home/<user>/.hermes/profiles/<p> --job-dir runtime/autotask/jobs [--dry-run]

The store is a dict with a ``jobs`` list; every job carries a stable ``id``, so re-running
updates the same job instead of adding a twin. Jobs installed this way are marked
``origin: autotask:<id>`` so a human job is never rewritten by accident.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import taskfile  # noqa: E402

CRON_EXPR = re.compile(r"^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)$")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")


def _job_id(slug: str) -> str:
    return hashlib.sha256(("autotask:" + slug).encode()).hexdigest()[:12]


def _schedule(expr: str) -> dict:
    return {"kind": "cron", "expr": expr, "display": "cron %s" % expr}


def _substitute(value, profile_path, date):
    if isinstance(value, str):
        return value.replace("<profile>", profile_path).replace("<date>", date)
    if isinstance(value, list):
        return [_substitute(v, profile_path, date) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, profile_path, date) for k, v in value.items()}
    return value


def build(task, *, profile_path, deliver="", workdir="", date=None):
    problems = taskfile.validate(task)
    if problems:
        raise ValueError("; ".join(problems))
    date = date or time.strftime("%Y-%m-%d")
    # validate the template (generic placeholders), then render the prompt with real paths
    rendered = taskfile.render(task).replace("<profile>", profile_path).replace("<date>", date)
    slug = _slug(task["id"])
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return {
        "id": _job_id(slug),
        "name": task["id"],
        "prompt": rendered,
        "skills": "[]",
        "skill": "None",
        "model": "None",
        "provider": "None",
        "base_url": "None",
        "script": "None",
        "no_agent": "False",
        "monitor_script": "None",
        "monitor_url": "None",
        "monitor_state": "None",
        "context_from": "None",
        "schedule": _schedule(task["schedule"]),
        "schedule_display": "cron %s" % task["schedule"],
        "repeat": {"times": None, "completed": 0},
        "enabled": True,
        "state": "scheduled",
        "paused_at": "None",
        "paused_reason": "None",
        "created_at": now,
        # The host computes next_run_at on its next tick (cron/job_definition.py); a guessed
        # value would schedule the job at the wrong instant or never.
        "next_run_at": None,
        "last_run_at": "None",
        "last_status": "None",
        "last_error": "None",
        "last_delivery_error": "None",
        "last_delivery_unverified": "None",
        "last_failure": "None",
        "failure_streak": "0",
        "deliver": "feishu:" + deliver if deliver else "None",
        "origin": "autotask:" + task["id"],
        "enabled_toolsets": "None",
        "workdir": workdir or profile_path,
        "fire_claim": "None",
        "canonical_completions": [],
        "last_dispatch": "None",
    }


def create_commands(tasks, *, profile, profile_path="", deliver="", workdir="", hermes="hermes",
                    date_token="<date>"):
    """Official-CLI commands to create these jobs (`cron create` is not idempotent by name).

    The host computes next_run_at only for jobs it creates itself, so this — not a jobs.json
    edit — is the supported way to enable an autotask job on a live profile.
    """
    import shlex
    home = Path(os.path.expanduser("~/.hermes"))
    resolved = profile_path or str(home / "profiles" / profile if profile else home)
    commands = []
    for task in tasks:
        prompt = taskfile.render(task).replace("<profile>", resolved).replace("<date>", date_token)
        target = deliver if task.get("notify") and deliver else "local"
        line = [hermes, "--profile", profile, "cron", "create", task["schedule"], prompt,
                "--name", task["id"], "--deliver", target]
        if workdir:
            line += ["--workdir", workdir]
        commands.append(" ".join(shlex.quote(part) for part in line))
    return commands


def install(store_path: Path, tasks, *, profile_path, deliver="", workdir="", dry_run=False):
    store = json.loads(store_path.read_text(encoding="utf-8")) if store_path.exists() else {}
    jobs = store.get("jobs") if isinstance(store, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("cron store has an unexpected shape: %s" % store_path)
    rows = []
    for task in tasks:
        new = build(task, profile_path=profile_path, deliver=deliver, workdir=workdir)
        index = next((i for i, job in enumerate(jobs) if job.get("id") == new["id"]), None)
        if index is None:
            owner = next((job for job in jobs if job.get("name") == new["name"]), None)
            if owner is not None:
                rows.append(("CONFLICT", new["name"], "an existing job has the same name but another id"))
                continue
            jobs.append(new)
            rows.append(("ADD", new["name"], new["schedule_display"]))
            continue
        if not str(jobs[index].get("origin") or "").startswith("autotask:"):
            rows.append(("CONFLICT", new["name"], "existing job was created by a human"))
            continue
        merged = dict(jobs[index], **{k: new[k] for k in ("prompt", "schedule", "schedule_display",
                                                          "deliver", "workdir")})
        if merged == jobs[index]:
            rows.append(("UNCHANGED", new["name"], new["schedule_display"]))
            continue
        jobs[index] = merged
        rows.append(("UPDATE", new["name"], new["schedule_display"]))
    if not dry_run and any(row[0] in {"ADD", "UPDATE"} for row in rows):
        store["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S.%f%z")
        tmp = store_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp, store_path.stat().st_mode & 0o777 if store_path.exists() else 0o600)
        os.replace(tmp, store_path)
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cron-file", required=True)
    parser.add_argument("--profile-path", required=True, help="absolute profile dir (<profile> substitution)")
    parser.add_argument("--job-dir", default=str(Path(__file__).resolve().parents[2] / "runtime/autotask/jobs"))
    parser.add_argument("--deliver", default="", help="feishu chat id for notifications (optional)")
    parser.add_argument("--workdir", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--create-commands", action="store_true",
                        help="print the official `cron create` commands instead of editing jobs.json")
    args = parser.parse_args(argv)
    tasks = [taskfile.load(p) for p in sorted(Path(args.job_dir).glob("*.json"))]
    if args.create_commands:
        for command in create_commands(tasks, profile=Path(args.profile_path).name,
                                       profile_path=args.profile_path,
                                       deliver=args.deliver, workdir=args.workdir):
            print(command)
        return 0
    rows = install(Path(args.cron_file), tasks, profile_path=args.profile_path,
                   deliver=args.deliver, workdir=args.workdir, dry_run=args.dry_run)
    for status, name, detail in rows:
        print("%-9s %-26s %s" % (status, name, detail))
    return 1 if any(row[0] == "CONFLICT" for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
