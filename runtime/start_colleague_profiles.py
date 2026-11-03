#!/usr/bin/env python3
"""Start explicitly listed Hermes profile gateways in a verified sequence."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys
import time


def start_profiles(names, *, profiles_root, identify, run=subprocess.run, timeout=60):
    if not names or len(set(names)) != len(names):
        raise ValueError("unique_profiles_required")
    root = Path(profiles_root).resolve(strict=True)
    for name in names:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
            raise ValueError("invalid_profile_name")
        home = root / name
        if home.is_symlink() or not home.is_dir():
            raise ValueError("profile_home_unavailable")
        unit = f"hermes-gateway-{name}.service"
        result = run(["systemctl", "--user", "start", unit], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(f"profile_start_failed:{name}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                info = identify(home, timeout=2)
                entries = info.get("served_profiles") if isinstance(info, dict) else None
                if isinstance(entries, list) and sum(
                    isinstance(item, dict) and item.get("home") == str(home) for item in entries
                ) == 1:
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            raise RuntimeError(f"profile_identity_unconfirmed:{name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-root", required=True, type=Path)
    parser.add_argument("--profiles-root", required=True, type=Path)
    parser.add_argument("profiles", nargs="+")
    args = parser.parse_args()
    root = args.hermes_root.resolve(strict=True)
    sys.path.insert(0, str(root))
    from hermes_cli.gateway_runtime_discovery import query_identify
    start_profiles(args.profiles, profiles_root=args.profiles_root, identify=query_identify)


if __name__ == "__main__":
    main()
