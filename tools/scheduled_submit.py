#!/usr/bin/env python3
"""CLI entrypoint for enqueuing scheduled durable tasks without an active agent conversation."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys
import yaml

MAX_GOAL_BYTES = 64 * 1024  # 64 KiB


def _load_durable_dispatch(repo_root: Path):
    try:
        from plugins.durable_dispatch import build_dispatch_services
        return build_dispatch_services
    except ImportError:
        plugin_init = repo_root / "plugins" / "durable_dispatch" / "__init__.py"
        spec = importlib.util.spec_from_file_location("durable_dispatch_plugin", plugin_init)
        if spec is None or spec.loader is None:
            raise ImportError("cannot load durable_dispatch plugin")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.build_dispatch_services


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Enqueue a scheduled durable task into a colleague queue."
    )
    parser.add_argument("--profile-home", required=True, type=Path,
                        help="Path to the profile home directory containing config.yaml")
    parser.add_argument("--role", required=True,
                        help="Colleague role to dispatch to (e.g. dev)")
    parser.add_argument("--route", required=True,
                        help="Administrator-registered route name in scheduled_routes")
    parser.add_argument("--goal-file", required=True, type=Path,
                        help="Path to UTF-8 file containing the task goal (<= 64 KiB)")
    parser.add_argument("--idempotency-key", required=True,
                        help="Stable key for task deduplication (1-128 chars)")
    parser.add_argument("--expect-new", action="store_true", default=False,
                        help="Refuse if the task was already deduplicated instead of newly enqueued")

    args = parser.parse_args(argv)

    profile_home = args.profile_home
    if not profile_home.is_dir():
        sys.stderr.write(f"profile-home directory not found: {profile_home}\n")
        sys.exit(2)

    config_file = profile_home / "config.yaml"
    if not config_file.is_file():
        sys.stderr.write(f"config.yaml not found in profile-home: {config_file}\n")
        sys.exit(2)

    try:
        config_text = config_file.read_text(encoding="utf-8")
        config_data = yaml.safe_load(config_text)
    except Exception as exc:
        sys.stderr.write(f"failed to parse config.yaml: {exc}\n")
        sys.exit(2)

    if not isinstance(config_data, dict):
        sys.stderr.write("invalid config.yaml: root must be a mapping\n")
        sys.exit(2)

    plugins = config_data.get("plugins")
    if not isinstance(plugins, dict):
        sys.stderr.write("missing or invalid 'plugins' section in config.yaml\n")
        sys.exit(2)

    entries = plugins.get("entries")
    if not isinstance(entries, dict):
        sys.stderr.write("missing or invalid 'plugins.entries' section in config.yaml\n")
        sys.exit(2)

    dd_entry = entries.get("durable-dispatch")
    if not isinstance(dd_entry, dict):
        sys.stderr.write("durable-dispatch plugin not configured in config.yaml\n")
        sys.exit(2)

    settings = dd_entry.get("settings")
    if not isinstance(settings, dict):
        sys.stderr.write("durable-dispatch plugin settings missing or invalid in config.yaml\n")
        sys.exit(2)

    if settings.get("enabled") is not True:
        sys.stderr.write("durable-dispatch plugin is not enabled\n")
        sys.exit(2)

    if settings.get("deliver_completions") is not True:
        sys.stderr.write("completion delivery must be enabled (deliver_completions: true) for scheduled routes\n")
        sys.exit(2)

    goal_file = args.goal_file
    if not goal_file.is_file():
        sys.stderr.write(f"goal file not found: {goal_file}\n")
        sys.exit(2)

    try:
        stat_size = goal_file.stat().st_size
    except OSError as exc:
        sys.stderr.write(f"cannot inspect goal file: {exc}\n")
        sys.exit(2)

    if stat_size > MAX_GOAL_BYTES:
        sys.stderr.write(f"goal file exceeds size limit of {MAX_GOAL_BYTES} bytes\n")
        sys.exit(2)

    try:
        goal_content = goal_file.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        sys.stderr.write(f"goal file is not valid UTF-8: {exc}\n")
        sys.exit(2)

    if not goal_content.strip() or len(goal_content) > MAX_GOAL_BYTES:
        sys.stderr.write("goal content must be non-empty and <= 64 KiB\n")
        sys.exit(2)

    key = args.idempotency_key
    if not isinstance(key, str) or not key.strip() or len(key) > 128:
        sys.stderr.write("idempotency-key must be a non-empty string of 1 to 128 characters\n")
        sys.exit(2)

    profile_name = config_data.get("profile") or config_data.get("profile_name") or profile_home.resolve().name
    if not isinstance(profile_name, str) or not profile_name.strip():
        sys.stderr.write("could not determine profile name\n")
        sys.exit(2)

    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    build_dispatch_services = _load_durable_dispatch(repo_root)

    try:
        services = build_dispatch_services(settings, profile_name)
    except ValueError as exc:
        sys.stderr.write(f"invalid configuration: {exc}\n")
        sys.exit(2)
    except PermissionError as exc:
        sys.stderr.write(f"permission denied: {exc}\n")
        sys.exit(3)
    except Exception as exc:
        sys.stderr.write(f"failed to initialize dispatch service: {exc}\n")
        sys.exit(2)

    if args.role not in services:
        sys.stderr.write(f"colleague role not configured: {args.role}\n")
        sys.exit(3)

    target_service = services[args.role]

    try:
        result = target_service.submit_scheduled(
            goal_content,
            route_name=args.route,
            idempotency_key=key,
        )
    except PermissionError as exc:
        sys.stderr.write(f"permission denied: {exc}\n")
        sys.exit(3)
    except ValueError as exc:
        sys.stderr.write(f"invalid argument: {exc}\n")
        sys.exit(2)
    except Exception as exc:
        sys.stderr.write(f"failed to enqueue scheduled task: {exc}\n")
        sys.exit(2)

    if args.expect_new and result.get("deduplicated"):
        sys.stderr.write(f"expected new task, but task was deduplicated: {result.get('task_id')}\n")
        sys.exit(2)

    output = {
        "ok": bool(result.get("ok")),
        "task_id": result.get("task_id"),
        "status": result.get("status"),
        "deduplicated": bool(result.get("deduplicated")),
    }
    print(json.dumps(output, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
