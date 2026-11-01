#!/usr/bin/env python3
"""Prepare a single-principal memory worker and matching localvec recall configuration."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

import yaml
from configure_defaults import add_missing, ensure_toolset, load
from setup_worker import quote_systemd, preflight_model


def make_plan(home, engine, profile, user, agent, embed_url, python, unit_dir):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", profile):
        raise ValueError("invalid profile")
    for value in (user, agent):
        if not value or len(value) > 512 or any(c in value for c in "\r\n\0"):
            raise ValueError("invalid memory principal")
    if not python.is_absolute() or not python.is_file():
        raise ValueError("absolute Python interpreter required")
    for relative in ("runtime/memory_bootstrap.py", "tools/recall_memory.py", "agentcore/memory/bge_sidecar.py"):
        if not (engine / relative).is_file():
            raise ValueError("memory runtime asset missing: " + relative)
    # Validate the URL without contacting it or importing an untrusted path.
    sys.path.insert(0, str(engine))
    from agentcore.memory.bge_sidecar import checked_url
    checked_url(embed_url)
    state = home / "state" / "superagent-memory"
    data, spool, queue = state / "data", state / "spool", state / "events.db"
    for path in (state, data, spool):
        if path.is_symlink() or (path.exists() and
                (not path.is_dir() or path.stat().st_mode & 0o077 or
                 (hasattr(os, "getuid") and path.stat().st_uid != os.getuid()))):
            raise ValueError("private memory directory required")
    config_path = home / "config.yaml"
    original = config_path.read_text(encoding="utf-8")
    load(original)
    cfg = yaml.safe_load(original) or {}
    plugins = cfg.get("plugins") or {}
    if "memory-bridge" not in (plugins.get("enabled") or []) or "memory-bridge" in (plugins.get("disabled") or []):
        raise ValueError("memory-bridge must be enabled")
    memory = cfg.get("memory") or {}
    if not isinstance(memory, dict) or memory.get("provider", "localvec") != "localvec":
        raise ValueError("existing memory provider differs; migration required")
    localvec = memory.get("localvec") or {}
    if not isinstance(localvec, dict):
        raise ValueError("invalid localvec configuration")
    recall = [str(python), str(engine / "tools/recall_memory.py"), "--query-text={query}",
              "--data-dir", str(data), "--user", user, "--agent", agent,
              "--profile", profile, "--embed-url", embed_url, "--limit", "{limit}"]
    if "recall_cmd" in localvec and localvec["recall_cmd"] != recall:
        raise ValueError("existing recall command differs; migration required")
    settings = (((plugins.get("entries") or {}).get("memory-bridge") or {}).get("settings") or {})
    expected = {"enabled": True, "engine_root": str(engine), "spool_dir": str(spool),
                "data_dir": str(data), "embed_url": embed_url, "user_id": user, "agent_id": agent}
    for key, value in expected.items():
        if key in settings and settings[key] != value:
            raise ValueError("existing memory bridge setting conflicts: " + key)
    updated = original
    for key, value in expected.items():
        updated = add_missing(updated, ["plugins", "entries", "memory-bridge", "settings", key], value)
    for key, value in (("provider", "localvec"),):
        updated = add_missing(updated, ["memory", key], value)
    fallback = {"recall_cmd": recall, "limit": 5, "timeout_s": 15,
                "sqlite_db": str(data / "coding_memory_cv.db"),
                "user_id": user, "agent_id": agent, "profile": profile}
    for key, value in fallback.items():
        if key in localvec and localvec[key] != value:
            raise ValueError("existing localvec setting conflicts: " + key)
        updated = add_missing(updated, ["memory", "localvec", key], value)
    updated = ensure_toolset(updated, "memory-bridge", "memory_bridge")
    environment = {"PYTHONPATH": str(engine), "HERMES_HOME": str(home),
                   "SUPERAGENT_MEMORY_DATA_DIR": str(data), "SUPERAGENT_MEMORY_USER": user,
                   "SUPERAGENT_MEMORY_AGENT": agent, "SUPERAGENT_MEMORY_EMBED_URL": embed_url}
    argv = [str(python), "-m", "agentcore.memory.runtime_service",
            "--bootstrap", "runtime.memory_bootstrap:bootstrap",
            "--spool", str(spool), "--queue", str(queue),
            "--profile", profile, "--user", user, "--agent", agent]
    unit = ("[Unit]\nDescription=Super Agent memory worker for " + profile +
            "\nAfter=network-online.target\n\n[Service]\nType=simple\nUMask=0077\n" +
            "".join("Environment=" + quote_systemd(k + "=" + v) + "\n" for k, v in environment.items()) +
            "ExecStart=" + " ".join(map(quote_systemd, argv)) +
            "\nRestart=on-failure\nRestartSec=5\nKillMode=control-group\nTimeoutStopSec=45\n"
            "\n[Install]\nWantedBy=default.target\n")
    return {"config": config_path, "original": original, "updated": updated,
            "unit_path": unit_dir / ("superagent-memory-" + profile + ".service"),
            "unit": unit, "state": state, "data": data, "spool": spool,
            "env": environment}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profile-home", type=Path, required=True)
    p.add_argument("--engine-root", type=Path, required=True)
    p.add_argument("--profile", required=True)
    p.add_argument("--user", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--embed-url", required=True)
    p.add_argument("--python", type=Path, default=Path("/usr/bin/python3"))
    p.add_argument("--unit-dir", type=Path, default=Path.home() / ".config/systemd/user")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--activate", action="store_true")
    args = p.parse_args()
    if args.activate and not args.apply:
        p.error("--activate requires --apply")
    if args.activate and args.unit_dir.resolve() != (Path.home() / ".config/systemd/user").resolve():
        p.error("--activate requires the standard user systemd unit directory")
    plan = make_plan(args.profile_home.resolve(), args.engine_root.resolve(), args.profile,
                     args.user, args.agent, args.embed_url, args.python.resolve(), args.unit_dir.resolve())
    unit = plan["unit_path"]
    if unit.is_symlink() or (unit.exists() and (not unit.is_file() or unit.read_text(encoding="utf-8") != plan["unit"])):
        raise ValueError("existing memory unit differs")
    print("memory state, principal and localvec recall planned for profile", args.profile)
    if not args.apply:
        return
    for directory in (plan["state"], plan["data"], plan["spool"], unit.parent):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Initialize an empty store only. Existing stores are never migrated.
    db = plan["data"] / "coding_memory_cv.db"
    if db.is_symlink() or (db.exists() and not db.is_file()):
        raise ValueError("memory database must be a regular file")
    if not db.exists():
        code = ("from runtime.memory_bootstrap import bootstrap; bootstrap(); "
                "from agentcore.memory.memory_store import _connect; c=_connect(); c.close()")
        env = dict(os.environ, **plan["env"])
        init = subprocess.run([str(args.python.resolve()), "-c", code], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if init.returncode:
            raise RuntimeError("memory store initialization failed")
    if not unit.exists():
        fd = os.open(unit, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(plan["unit"])
    if plan["updated"] != plan["original"]:
        fd, temporary = tempfile.mkstemp(prefix=".memory-defaults-", dir=plan["config"].parent)
        try:
            os.fchmod(fd, plan["config"].stat().st_mode & 0o777)
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write(plan["updated"])
                out.flush()
                os.fsync(out.fileno())
            shutil.copy2(plan["config"], plan["config"].with_name(plan["config"].name + ".before-memory-defaults"))
            os.replace(temporary, plan["config"])
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    if args.activate:
        service = "superagent-memory-" + args.profile + ".service"
        status = subprocess.run(["systemctl", "--user", "is-active", "--quiet", service])
        if status.returncode != 0:
            # Never consume an existing backlog implicitly.
            if any(plan["spool"].glob("*.json")):
                raise ValueError("memory spool contains existing turns; review before activation")
            queue_path = plan["state"] / "events.db"
            if queue_path.exists():
                with sqlite3.connect(queue_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
                    pending = db.execute("SELECT count(*) FROM turn_events WHERE state NOT IN ('done','failed')").fetchone()[0]
                if pending:
                    raise ValueError("memory queue contains pending turns; review before activation")
            preflight_model(args.profile_home.resolve(), args.engine_root.resolve(), args.python.resolve())
            environment = dict(os.environ, PYTHONPATH=str(args.engine_root.resolve()))
            probe_code = ("from agentcore.memory.bge_sidecar import make_embedder; "
                          "import sys; rows=make_embedder(sys.argv[1])(['synthetic readiness probe']); "
                          "raise SystemExit(0 if rows and len(rows[0]) == 1024 else 1)")
            probe = subprocess.run([str(args.python.resolve()), "-c", probe_code, args.embed_url],
                                   env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45)
            if probe.returncode:
                raise RuntimeError("embedding backend unavailable or wrong dimension")
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "--user", "enable", "--now", service], check=True)
            subprocess.run(["systemctl", "--user", "is-active", "--quiet", service], check=True)
        print("memory worker active; gateway reload required for plugin configuration")
    else:
        print("memory configuration and unit prepared; service and gateway not started")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, yaml.YAMLError, sqlite3.Error,
            subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        sys.exit("memory setup failed: " + str(exc))
