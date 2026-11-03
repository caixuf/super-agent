#!/usr/bin/env python3
"""Apply the versioned Hermes card lifecycle overlay, refusing unknown trees."""
import argparse
import os
from pathlib import Path
import subprocess

ROOT = Path(os.environ.get("HERMES_AGENT_DIR") or (Path.home() / ".hermes/hermes-agent")).expanduser()
PATCH = Path(__file__).with_name("feishu_card_lifecycle.patch")

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
        print("card lifecycle host or patch missing")
        return 1
    if git("--check", "-R").returncode == 0:
        print("card lifecycle present")
        return 0
    if args.check:
        print("card lifecycle absent or incompatible")
        return 1
    if git("--check").returncode != 0:
        print("card lifecycle incompatible; no change made")
        return 1
    if args.preflight:
        print("card lifecycle applicable")
        return 0
    if git().returncode != 0 or git("--check", "-R").returncode != 0:
        print("card lifecycle application failed")
        return 1
    print("card lifecycle applied")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
