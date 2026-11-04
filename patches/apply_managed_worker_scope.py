#!/usr/bin/env python3
"""Apply opt-in managed-worker scope grouping; it does not establish quiescence."""
import argparse
import os
from pathlib import Path
import subprocess

ROOT = Path(os.environ.get("HERMES_AGENT_DIR") or (Path.home() / ".hermes/hermes-agent")).expanduser()
PATCH = Path(__file__).with_name("managed_worker_scope.patch")

def git(*args):
    return subprocess.run(["git", "apply", *args, str(PATCH)], cwd=ROOT, capture_output=True, text=True)

def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--preflight", action="store_true")
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not ROOT.is_dir() or not PATCH.is_file():
        print("scope host or patch missing"); return 1
    if git("--check", "-R").returncode == 0:
        print("scope grouping patch present"); return 0
    if args.check:
        print("scope grouping patch absent or incompatible"); return 1
    if git("--check").returncode != 0:
        print("scope grouping patch incompatible; no change made"); return 1
    if args.preflight:
        print("scope grouping patch applicable"); return 0
    if git().returncode != 0 or git("--check", "-R").returncode != 0:
        print("scope grouping patch application failed"); return 1
    print("scope grouping patch applied"); return 0

if __name__ == "__main__":
    raise SystemExit(main())
