#!/usr/bin/env python3
"""Plan and safely apply the receptionist/worker model split for a Hermes profile.

Default is read-only. ``--apply`` backs up config and uses Hermes' official
``config set`` commands; it never edits YAML directly or restarts the gateway.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

REPO = Path(__file__).resolve().parents[1]


def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, env=env or os.environ.copy())


def _value(hermes: str, profile: str, key: str) -> str:
    proc = _run([hermes, "--profile", profile, "config", "get", key])
    return proc.stdout.strip() if proc.returncode == 0 else ""


def build_plan(profile_home: Path, profile: str, hermes: str) -> dict[str, Any]:
    import yaml
    config_path = profile_home / "config.yaml"
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    model = data.get("model") or {}
    delegation = data.get("delegation") or {}
    model_map = ((delegation.get("backends") or {}).get("model_map") or {})
    display = data.get("display") or {}
    plugins = data.get("plugins") or {}
    colleague = (((plugins.get("entries") or {}).get("colleague-team") or {}).get("settings") or {})
    durable = (((plugins.get("entries") or {}).get("durable-dispatch") or {}).get("settings") or {})
    options = {item.get("name"): item for item in model_map.get("options", []) if isinstance(item, dict)}
    desired = {
        "model.default": "flash",
        "delegation.backends.model_map.default": "deep",
        "display.busy_input_mode": "steer",
    }
    current = {
        "foreground_model": model.get("default"),
        "foreground_provider": model.get("provider"),
        "worker_default": model_map.get("default"),
        "worker_fast": options.get("fast", {}).get("model"),
        "worker_deep": options.get("deep", {}).get("model"),
        "worker_architect": options.get("architect", {}).get("model"),
        "busy_input_mode": display.get("busy_input_mode", "interrupt"),
        "colleague_dispatch": colleague.get("gateway_dispatch", {}).get("enabled", False),
        "deliver_completions": durable.get("deliver_completions", False),
    }
    blockers = []
    for required in ("fast", "deep", "architect"):
        if required not in options:
            blockers.append("missing_delegation_route_" + required)
    if current["foreground_provider"] != "litellm":
        blockers.append("foreground_provider_not_litellm")
    changes = {key: {"from": _value(hermes, profile, key), "to": value}
               for key, value in desired.items()
               if _value(hermes, profile, key) != value}
    return {
        "schema_version": 1,
        "profile": profile,
        "profile_home": str(profile_home),
        "config": str(config_path),
        "current": current,
        "desired": desired,
        "changes": changes,
        "blockers": blockers,
        "runtime_actions_after_apply": [
            "wait for active work to drain",
            "restart hermes-gateway-" + profile + ".service",
            "verify one gateway process and Feishu connected",
            "send a private test message and verify first response plus worker dispatch",
        ],
        "worker_execution_still_to_verify": {
            "colleague_dispatch": current["colleague_dispatch"],
            "deliver_completions": current["deliver_completions"],
        },
    }


def apply(plan: dict[str, Any], hermes: str) -> int:
    profile_home = Path(plan["profile_home"])
    config = Path(plan["config"])
    backup_root = profile_home / "backups" / "config"
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = backup_root / ("config.yaml.receptionist-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(config, backup)
    for key, change in plan["changes"].items():
        proc = _run([hermes, "--profile", plan["profile"], "config", "set", key, change["to"]])
        if proc.returncode != 0:
            shutil.copy2(backup, config)
            print(json.dumps({"status": "failed", "key": key, "error": proc.stderr[-500:],
                              "restored": str(config), "backup": str(backup)}, indent=2))
            return 1
    print(json.dumps({"status": "applied", "backup": str(backup),
                      "changes": plan["changes"], "restart_required": True,
                      "next": plan["runtime_actions_after_apply"]}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", required=True)
    parser.add_argument("--profile-home", type=Path, required=True)
    parser.add_argument("--hermes", default=str(Path.home() / ".local/bin/hermes"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    plan = build_plan(args.profile_home.expanduser(), args.profile, args.hermes)
    if not args.apply:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0 if not plan["blockers"] else 1
    if plan["blockers"]:
        print(json.dumps({"status": "blocked", "blockers": plan["blockers"]}, indent=2))
        return 1
    return apply(plan, args.hermes)


if __name__ == "__main__":
    raise SystemExit(main())
