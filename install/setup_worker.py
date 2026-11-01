#!/usr/bin/env python3
"""Generate a profile-specific durable analysis worker from existing dispatch policy.

Without --activate, no queue is consumed or service started. Existing differing
files are refused, so operator-managed configuration is never overwritten.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required to read the profile")


def quote_systemd(value: str) -> str:
    if any(c in value for c in "\n\r\0"):
        raise ValueError("newline or NUL in path")
    return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def plan(home: Path, engine: Path, unit_dir: Path, profile: str, python: Path):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", profile):
        raise ValueError("profile name must contain only letters, numbers, underscore or hyphen")
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
    plugins = config.get("plugins") or {}
    if "durable-dispatch" not in (plugins.get("enabled") or []) or "durable-dispatch" in (plugins.get("disabled") or []):
        raise ValueError("durable-dispatch must be enabled")
    settings = (((plugins.get("entries") or {}).get("durable-dispatch") or {}).get("settings") or {})
    if settings.get("enabled") is not True:
        raise ValueError("durable-dispatch settings.enabled must be true")
    root = settings.get("queue_root")
    roots = settings.get("allowed_roots")
    denied = settings.get("denied_globs", [])
    if not isinstance(root, str) or not Path(root).is_absolute():
        raise ValueError("queue_root must be an absolute path")
    if not isinstance(roots, list) or not roots or not all(isinstance(p, str) and Path(p).is_absolute() and Path(p).is_dir() for p in roots):
        raise ValueError("allowed_roots must contain existing absolute directories")
    if not isinstance(denied, list) or not all(isinstance(p, str) for p in denied):
        raise ValueError("denied_globs must be a list of strings")
    # A running plugin may intentionally use an existing canonical checkout.
    # The worker must use that same engine until the operator migrates both.
    configured_engine = settings.get("engine_root")
    if configured_engine:
        if not isinstance(configured_engine, str) or not Path(configured_engine).is_absolute():
            raise ValueError("configured engine_root must be absolute")
        engine = Path(configured_engine).resolve()
    if not (engine / "agentcore" / "analysis_runner.py").is_file():
        raise ValueError("engine lacks analysis_runner.py")
    if not python.is_absolute() or not python.is_file():
        raise ValueError("python must be an existing absolute executable")
    directory = home / "worker"
    policy = directory / "analysis-policy.json"
    runner = directory / "runner.json"
    policy_data = {"allowed_roots": roots, "denied_globs": denied,
                   "model_options": {"alias": "flash", "timeout_seconds": 90},
                   "fallback_model_options": {"alias": "main", "timeout_seconds": 90}}
    runner_data = [str(python), str(engine / "agentcore" / "analysis_runner.py"),
                   "--policy", str(policy), "--model-adapter", "agentcore.ccp_analysis_model:model"]
    argv = [str(python), "-m", "agentcore.pool", "--root", root, "worker",
            "--runner-config", str(runner), "--workers", "1", "--backend", "stdlib"]
    unit = ("[Unit]\nDescription=Super Agent durable worker for " + profile +
            "\nAfter=network-online.target\n\n[Service]\nType=simple\nUMask=0077\n"
            + "Environment=" + quote_systemd("PYTHONPATH=" + str(engine)) + "\n"
            + "Environment=" + quote_systemd("HERMES_HOME=" + str(home)) + "\n"
            + "ExecStart=" + " ".join(map(quote_systemd, argv)) +
            "\nRestart=on-failure\nRestartSec=5\nKillMode=control-group\nTimeoutStopSec=30\n"
            "\n[Install]\nWantedBy=default.target\n")
    return {policy: json.dumps(policy_data, ensure_ascii=False, indent=2) + "\n",
            runner: json.dumps(runner_data, ensure_ascii=False, indent=2) + "\n",
            unit_dir / ("superagent-worker-" + profile + ".service"): unit}



def preflight_model(home: Path, engine: Path, python: Path):
    # Match the user manager's environment instead of relying on shell-only secrets.
    result = subprocess.run(["systemctl", "--user", "show-environment"], check=True,
                            capture_output=True, text=True, timeout=10)
    env = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            env[key] = value
    env.setdefault("HOME", str(Path.home()))
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(engine)
    # No credential, URL or provider response is printed or added to argv.
    code = (
        "from agentcore.ccp_config import resolve_ccp_credentials; "
        "from agentcore.ccp_analysis_model import _safe_endpoint, _open_request; "
        "from urllib.request import Request; import json; "
        "c=resolve_ccp_credentials(); u=_safe_endpoint(c.base_url); "
        "r=_open_request(Request(u.removesuffix('/chat/completions')+'/models', "
        "headers={'Authorization':'Bearer '+c.api_key}), 5); "
        "data=json.loads(r.read(512001)); r.close(); "
        "ids={x.get('id') for x in data.get('data',[]) if isinstance(x,dict)}; "
        "assert {'flash','main'} <= ids"
    )
    probe = subprocess.run([str(python), "-c", code], env=env, capture_output=True, timeout=10)
    if probe.returncode:
        raise ValueError("worker model credentials, endpoint or flash/main routes unavailable in the service environment")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profile-home", type=Path, required=True)
    p.add_argument("--engine-root", type=Path, required=True)
    p.add_argument("--profile", required=True)
    p.add_argument("--unit-dir", type=Path, default=Path.home() / ".config/systemd/user")
    p.add_argument("--python", type=Path, default=Path("/usr/bin/python3"))
    p.add_argument("--apply", action="store_true")
    p.add_argument("--activate", action="store_true", help="activate generated user service after checking queued work")
    args = p.parse_args()
    if args.activate and not args.apply:
        p.error("--activate requires --apply")
    if args.activate and args.unit_dir.resolve() != (Path.home() / ".config/systemd/user").resolve():
        p.error("--activate requires the standard user systemd unit directory")
    files = plan(args.profile_home.resolve(), args.engine_root.resolve(), args.unit_dir.resolve(),
                 args.profile, args.python.resolve())
    for path, content in files.items():
        if path.exists() and (not path.is_file() or path.read_text(encoding="utf-8") != content):
            raise ValueError("refusing to replace existing different file: " + str(path))
    for path, content in files.items():
        if args.apply and not path.exists():
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
        print(("ready " if path.exists() else "would create ") + str(path))
    if args.activate:
        config = yaml.safe_load((args.profile_home / "config.yaml").read_text(encoding="utf-8"))
        queue = config["plugins"]["entries"]["durable-dispatch"]["settings"]["queue_root"]
        database = Path(queue) / "pool.db"
        if database.exists():
            with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
                active = db.execute("SELECT count(*) FROM jobs WHERE status NOT IN ('completed','failed','cancelled')").fetchone()[0]
            if active:
                raise ValueError("queue contains nonterminal work; inspect it before starting a new worker")
        engine_arg = config["plugins"]["entries"]["durable-dispatch"]["settings"].get("engine_root") or str(args.engine_root.resolve())
        preflight_model(args.profile_home.resolve(), Path(engine_arg).resolve(), args.python.resolve())
        name = "superagent-worker-" + args.profile + ".service"
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", name], check=True)
        subprocess.run(["systemctl", "--user", "is-active", "--quiet", name], check=True)
        print("worker service active: " + name)
    else:
        print("Not started: verify CCP credentials and endpoint, then daemon-reload and enable the unit.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, yaml.YAMLError, sqlite3.Error, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        sys.exit("worker setup failed: " + str(e))
