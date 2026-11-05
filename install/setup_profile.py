#!/usr/bin/env python3
"""CLI utility to inspect, render, install, package, or uninstall a business profile.

Super Agent remains completely neutral: this tool reads a domain profile
specification and configures systemd user units, queue directories, and
watchdog timers accordingly.

Usage:
  python3 -m install.setup_profile inspect <spec-path>
  python3 -m install.setup_profile render  <spec-path> [--out-dir DIR]
  python3 -m install.setup_profile install <spec-path> [--unit-dir DIR] [--activate] [--dry-run]
  python3 -m install.setup_profile uninstall <spec-path> [--unit-dir DIR] [--apply]
  python3 -m install.setup_profile pack    <spec-path> --out <tar.gz>
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentcore.profile_spec import (
    BusinessProfileSpec,
    load_profile_file,
    package_profile_bundle,
)


def get_default_unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def cmd_inspect(spec_path: Path) -> int:
    spec = load_profile_file(spec_path)
    print(f"=== Super Agent Profile: {spec.profile_id} (v{spec.version}) ===")
    if spec.description:
        print(f"Description : {spec.description}")
    print(f"Profile Home: {spec.get_profile_home()}")
    print(f"Engine Root : {spec.get_engine_root()}")
    print(f"Python Exec : {spec.python_bin}")
    print(f"\nWorkers ({len(spec.workers)} defined):")
    for w in spec.workers:
        print(
            f"  - [{w.role}] tier={w.model_tier or 'flash'} "
            f"concurrency={w.concurrency} timeout={w.timeout}s max_file={w.max_file} bytes"
        )
        if w.duty:
            print(f"    Duty   : {w.duty}")
        if w.workdir:
            print(f"    Workdir: {w.workdir}")
        if w.target_host:
            print(f"    Host   : {w.target_host}")

    if spec.proactive_hooks:
        print("\nProactive Hooks:")
        print(f"  Config  : {spec.proactive_hooks.config_file}")
        print(f"  Schedule: {spec.proactive_hooks.timer_schedule}")

    if spec.memory:
        print("\nMemory Watcher:")
        print(f"  Source  : {spec.memory.source}")
        print(f"  User    : {spec.memory.user}")
        print(f"  Agent   : {spec.memory.agent}")

    units = spec.render_all_units()
    print(f"\nPlanned Systemd Units ({len(units)} total):")
    for uname in sorted(units.keys()):
        print(f"  * {uname}")
    return 0


def cmd_render(spec_path: Path, out_dir: Optional[Path]) -> int:
    spec = load_profile_file(spec_path)
    units = spec.render_all_units()
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        for uname, content in sorted(units.items()):
            dest = out_dir / uname
            dest.write_text(content, encoding="utf-8")
            print(f"[WROTE] {dest}")
        print(f"Rendered {len(units)} units to {out_dir}")
    else:
        for uname, content in sorted(units.items()):
            print(f"### File: {uname} ###")
            print(content)
            print("-" * 60)
    return 0


def cmd_install(spec_path: Path, unit_dir: Path, activate: bool, dry_run: bool) -> int:
    spec = load_profile_file(spec_path)
    units = spec.render_all_units()

    print(f"[INFO] Profile: {spec.profile_id}")
    print(f"[INFO] Target unit directory: {unit_dir}")

    # Ensure queue directories
    for w in spec.workers:
        q_root = spec.get_queue_root(w.role)
        print(f"[PLAN] Queue root: {q_root}")
        if not dry_run:
            q_root.mkdir(parents=True, exist_ok=True)

    # Ensure default runner config if missing
    runner_config_path = spec.get_profile_home() / "worker" / "dev-runner.json"
    if not dry_run and not runner_config_path.exists():
        runner_config_path.parent.mkdir(parents=True, exist_ok=True)
        runner_config_path.write_text(
            json.dumps({"command": [spec.python_bin, "-m", "agentcore.dev_runner"]}, indent=2),
            encoding="utf-8"
        )
        print(f"[CREATE] Initial runner config at {runner_config_path}")

    # Write unit files
    for uname, content in sorted(units.items()):
        dest = unit_dir / uname
        print(f"[PLAN] Install unit: {dest}")
        if not dry_run:
            unit_dir.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")

    if dry_run:
        print("[DRY-RUN] No systemd units written or activated.")
        return 0

    print(f"[OK] Installed {len(units)} unit files into {unit_dir}")

    if activate:
        print("[INFO] Reloading systemd daemon and enabling units...")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        for uname in sorted(units.keys()):
            if uname.endswith(".timer") or uname.endswith(".service"):
                subprocess.run(["systemctl", "--user", "enable", "--now", uname], check=False)
        print("[OK] Services enabled and activated.")
    else:
        print("[TIP] To activate services, run:")
        print("  systemctl --user daemon-reload")
        for uname in sorted(units.keys()):
            print(f"  systemctl --user enable --now {uname}")
    return 0


def cmd_uninstall(spec_path: Path, unit_dir: Path, apply: bool) -> int:
    spec = load_profile_file(spec_path)
    units = spec.render_all_units()

    print(f"[INFO] Uninstalling profile: {spec.profile_id}")
    for uname in sorted(units.keys()):
        dest = unit_dir / uname
        if dest.exists():
            print(f"[PLAN] Remove {dest}")
            if apply:
                subprocess.run(["systemctl", "--user", "stop", uname], check=False)
                subprocess.run(["systemctl", "--user", "disable", uname], check=False)
                dest.unlink()
                print(f"[REMOVED] {dest}")

    if not apply:
        print("[DRY-RUN] Pass --apply to stop and remove units.")
    else:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        print("[OK] Uninstallation complete.")
    return 0


def cmd_pack(spec_path: Path, out_path: Path) -> int:
    bundle_path = package_profile_bundle(spec_path, out_path)
    print(f"[OK] Packaged profile bundle: {bundle_path} ({bundle_path.stat().st_size} bytes)")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Super Agent Business Profile Scaffolding & Packaging Tool"
    )
    sub = parser.add_subparsers(dest="action", required=True)

    p_inspect = sub.add_parser("inspect", help="Inspect and validate a profile specification")
    p_inspect.add_argument("spec", type=Path, help="Path to profile.yaml or profile.json")

    p_render = sub.add_parser("render", help="Render systemd unit files without installing")
    p_render.add_argument("spec", type=Path, help="Path to profile.yaml or profile.json")
    p_render.add_argument("--out-dir", type=Path, default=None, help="Directory to output units")

    p_install = sub.add_parser("install", help="Install units and prepare queue directories")
    p_install.add_argument("spec", type=Path, help="Path to profile.yaml or profile.json")
    p_install.add_argument(
        "--unit-dir", type=Path, default=get_default_unit_dir(), help="Systemd user unit directory"
    )
    p_install.add_argument("--activate", action="store_true", help="Reload daemon and start units")
    p_install.add_argument("--dry-run", action="store_true", help="Print plan without modifying disk")

    p_uninstall = sub.add_parser("uninstall", help="Uninstall profile units")
    p_uninstall.add_argument("spec", type=Path, help="Path to profile.yaml or profile.json")
    p_uninstall.add_argument(
        "--unit-dir", type=Path, default=get_default_unit_dir(), help="Systemd user unit directory"
    )
    p_uninstall.add_argument("--apply", action="store_true", help="Apply removal")

    p_pack = sub.add_parser("pack", help="Package profile into a portable tar.gz bundle")
    p_pack.add_argument("spec", type=Path, help="Path to profile.yaml or profile.json")
    p_pack.add_argument("--out", type=Path, required=True, help="Destination archive path (.tar.gz)")

    args = parser.parse_args(argv)

    if args.action == "inspect":
        return cmd_inspect(args.spec)
    elif args.action == "render":
        return cmd_render(args.spec, args.out_dir)
    elif args.action == "install":
        return cmd_install(args.spec, args.unit_dir, args.activate, args.dry_run)
    elif args.action == "uninstall":
        return cmd_uninstall(args.spec, args.unit_dir, args.apply)
    elif args.action == "pack":
        return cmd_pack(args.spec, args.out)
    return 1


if __name__ == "__main__":
    sys.exit(main())
