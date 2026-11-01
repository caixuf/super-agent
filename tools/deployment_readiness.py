#!/usr/bin/env python3
"""Report deployment readiness without printing credentials or sending messages."""
from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path
import subprocess
import sys

import yaml


def inspect(home: Path, profile: str):
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
    plugins = config.get("plugins") or {}
    enabled = plugins.get("enabled") or []
    disabled = plugins.get("disabled") or []
    entries = plugins.get("entries") or {}
    results = []

    def add(name, ok, detail):
        results.append((name, bool(ok), detail))

    for name, required in {
        "task-status": ("enabled", "engine_root", "liveness_db"),
        "task-progress": ("enabled", "engine_root", "liveness_db"),
        "illustrated-delivery": ("enabled", "engine_root"),
        "durable-dispatch": ("enabled", "engine_root", "queue_root", "allowed_roots"),
        "markdown-delivery": ("enabled", "engine_root", "env_file", "outbox_root"),
        "memory-bridge": ("enabled", "engine_root", "spool_dir", "user_id", "agent_id"),
        "colleague-catalog": ("enabled", "engine_root", "roles"),
    }.items():
        entry = (entries.get(name) or {}).get("settings") or {}
        missing = [key for key in required if not entry.get(key)]
        registered = name in enabled and name not in disabled
        add(name, registered and not missing,
            ("missing: " + ", ".join(missing)) if missing else ("not enabled" if not registered else "configured; runtime probe still required"))

    durable = ((entries.get("durable-dispatch") or {}).get("settings") or {})
    try:
        status = subprocess.run(["systemctl", "--user", "is-active", "--quiet",
                                 "superagent-worker-" + profile + ".service"], timeout=8)
        active = status.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        active = False
    policy_path = home / "worker" / "analysis-policy.json"
    runner_path = home / "worker" / "runner.json"
    unit_path = Path.home() / ".config/systemd/user" / ("superagent-worker-" + profile + ".service")
    aligned = False
    if active:
        try:
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            runner = json.loads(runner_path.read_text(encoding="utf-8"))
            unit = unit_path.read_text(encoding="utf-8")
            configured_engine = Path(durable.get("engine_root", ""))
            command = next(line[len("ExecStart="):] for line in unit.splitlines() if line.startswith("ExecStart="))
            argv = shlex.split(command)
            root_index = argv.index("--root")
            service_queue = argv[root_index + 1].replace("%%", "%")
            aligned = (policy.get("allowed_roots") == durable.get("allowed_roots")
                       and policy.get("denied_globs", []) == durable.get("denied_globs", [])
                       and isinstance(runner, list) and len(runner) >= 2
                       and configured_engine.is_absolute()
                       and runner[1] == str(configured_engine.resolve() / "agentcore/analysis_runner.py")
                       and isinstance(durable.get("queue_root"), str)
                       and Path(durable["queue_root"]).is_absolute()
                       and service_queue == durable["queue_root"])
        except (OSError, ValueError, TypeError, StopIteration, IndexError):
            pass
    add("durable-worker", active and aligned,
        "active; saved queue and policy match; process state not checked" if active and aligned else
        ("service active but queue/policy alignment is not verified" if active else "service not verified active"))
    memory = config.get("memory") or {}
    localvec = memory.get("localvec") or {}
    recall = localvec.get("recall_cmd")
    if memory.get("provider") == "localvec" and isinstance(recall, list) and recall:
        scripts = [Path(part).expanduser() for part in recall if isinstance(part, str) and part.endswith(".py")]
        recall_configured = bool(scripts) and all(p.is_absolute() and p.is_file() for p in scripts)
    else:
        recall_configured = False
    add("memory-recall", recall_configured,
        "configured command exists; recall semantics and backend still require verification" if recall_configured
        else "localvec recall command is absent or its script is missing")
    try:
        worker = subprocess.run(["systemctl", "--user", "is-active", "--quiet",
                                 "superagent-memory-" + profile + ".service"], timeout=8)
        memory_worker_active = worker.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        memory_worker_active = False
    add("memory-ingestion-worker", memory_worker_active,
        "service active; store/principal alignment still requires verification" if memory_worker_active
        else "no managed automatic ingestion worker verified")
    aligned_memory = False
    if memory_worker_active and isinstance(recall, list):
        try:
            unit_path = Path.home() / ".config/systemd/user" / ("superagent-memory-" + profile + ".service")
            unit = unit_path.read_text(encoding="utf-8")
            env = {}
            command = []
            for line in unit.splitlines():
                if line.startswith("Environment="):
                    key, value = shlex.split(line[len("Environment="):])[0].split("=", 1)
                    env[key] = value.replace("%%", "%")
                elif line.startswith("ExecStart="):
                    command = [part.replace("%%", "%") for part in shlex.split(line[len("ExecStart="):])]
            def arg(argv, name):
                return argv[argv.index(name) + 1]
            bridge = ((entries.get("memory-bridge") or {}).get("settings") or {})
            aligned_memory = (
                env.get("SUPERAGENT_MEMORY_DATA_DIR") == arg(recall, "--data-dir")
                and env.get("SUPERAGENT_MEMORY_USER") == arg(recall, "--user") == bridge.get("user_id") == arg(command, "--user")
                and env.get("SUPERAGENT_MEMORY_AGENT") == arg(recall, "--agent") == bridge.get("agent_id") == arg(command, "--agent")
                and arg(recall, "--profile") == profile == arg(command, "--profile") == localvec.get("profile")
                and env.get("SUPERAGENT_MEMORY_EMBED_URL") == arg(recall, "--embed-url")
                and arg(command, "--spool") == bridge.get("spool_dir")
                and localvec.get("sqlite_db") == str(Path(arg(recall, "--data-dir")) / "coding_memory_cv.db")
            )
        except (OSError, ValueError, TypeError, IndexError, KeyError):
            pass
    add("memory-store-alignment", aligned_memory,
        "saved store/principal settings match; live behavior not verified" if aligned_memory else
        "ingestion-to-recall store and principal binding not verified")
    delivery = (durable.get("deliver_completions") is True and
                durable.get("direct_delivery_enabled") is True and
                bool(durable.get("delivery_transport_file")))
    add("completion-delivery", delivery, "configured; real platform delivery not verified" if delivery else "direct completion delivery is not configured")
    team = ((entries.get("colleague-team") or {}).get("settings") or {})
    add("colleague-team", False, "real provisioning and lifecycle integration is not established")
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-home", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    args = parser.parse_args()
    results = inspect(args.profile_home.resolve(), args.profile)
    for name, ok, detail in results:
        print(("[CONFIGURED]" if ok else "[OPEN]") + " " + name + ": " + detail)
    print("Configuration checks are not proof of real model, platform or end-to-end delivery.")
    return 0 if all(row[1] for row in results) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, yaml.YAMLError) as exc:
        sys.exit("readiness check failed: " + type(exc).__name__)
