#!/usr/bin/env python3
"""Synchronize a stale Hermes dependency environment before gateway startup."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-root", type=Path, required=True)
    parser.add_argument("--hermes-home", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    root, home = args.hermes_root, args.hermes_home
    if (not root.is_absolute() or not home.is_absolute() or not root.is_dir()
            or not home.is_dir() or not 1 <= args.timeout <= 1800):
        parser.error("absolute existing Hermes directories and valid timeout required")
    launcher = root / ".hermes/bin/hermes"
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        parser.error("Hermes launcher unavailable")
    # pm and the subprocess must agree on the target home; never fall back to
    # a shell's active profile when systemd starts an unattended host.
    os.environ["HERMES_HOME"] = str(home)
    sys.path.insert(0, str(root))
    import pm
    if pm.venv_is_current(project_root=root):
        return 0
    env = dict(os.environ, HERMES_DISABLE_LAZY_INSTALLS="1")
    subprocess.run([str(launcher), "pm", "install", "venv"], env=env,
                   check=True, timeout=args.timeout)
    if not pm.venv_is_current(project_root=root):
        raise RuntimeError("dependency_environment_still_stale")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        print("Hermes dependency preflight failed: " + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
