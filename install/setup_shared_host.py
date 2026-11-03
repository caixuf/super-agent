#!/usr/bin/env python3
"""Prepare an opt-in Hermes shared-host service with dependency preflight."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

from setup_worker import quote_systemd


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--hermes-root", type=Path, required=True)
    p.add_argument("--hermes-home", type=Path, required=True)
    p.add_argument("--state-dir", type=Path, required=True)
    p.add_argument("--unit-dir", type=Path, required=True)
    p.add_argument("--unit-name", default="hermes-shared-host.service")
    p.add_argument("--proxy", default="")
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()
    for path in (a.hermes_root, a.hermes_home, a.state_dir, a.unit_dir):
        if not path.is_absolute() or ".." in path.parts or "\n" in str(path):
            p.error("absolute paths without traversal required")
    if not re.fullmatch(r"[a-zA-Z0-9_.@-]+\.service", a.unit_name):
        p.error("invalid unit name")
    if not a.hermes_root.is_dir() or not a.hermes_home.is_dir():
        p.error("Hermes directories unavailable")
    launcher = a.hermes_root / ".hermes/bin/hermes"
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        p.error("Hermes launcher unavailable")
    if a.proxy:
        proxy = urlsplit(a.proxy)
        if proxy.scheme not in ("http", "https") or not proxy.hostname or proxy.username or proxy.password or proxy.path not in ("", "/") or proxy.query or proxy.fragment:
            p.error("proxy must be an HTTP(S) URL without credentials")
    preflight = a.state_dir / "hermes-pm-preflight.py"
    source = (Path(__file__).resolve().parents[1] / "tools/hermes_pm_preflight.py").read_text()
    pre = ["/usr/bin/env"]
    if a.proxy:
        pre += ["HTTP_PROXY=" + a.proxy, "HTTPS_PROXY=" + a.proxy]
    pre += [sys.executable, str(preflight), "--hermes-root", str(a.hermes_root), "--hermes-home", str(a.hermes_home)]
    unit = ("[Unit]\nDescription=Hermes shared profile gateway\nAfter=network-online.target\n\n"
            "[Service]\nType=simple\nEnvironment=" + quote_systemd("HERMES_HOME=" + str(a.hermes_home)) + "\n"
            + "ExecStartPre=" + " ".join(map(quote_systemd, pre)) + "\n"
            + "ExecStart=" + " ".join(map(quote_systemd, [str(launcher), "gateway", "run", "--force", "--external-supervisor"])) + "\n"
            + "Restart=on-failure\nRestartSec=5\nKillMode=control-group\nTimeoutStopSec=90\nTimeoutStartSec=660\n\n"
            + "[Install]\nWantedBy=default.target\n")
    targets = {preflight: source, a.unit_dir / a.unit_name: unit}
    for path, content in targets.items():
        if path.is_symlink() or (path.exists() and (not path.is_file() or path.read_text() != content)):
            raise ValueError("refusing to replace existing different file")
    if not a.apply:
        print("shared host files planned; service not activated")
        return 0
    for path, content in targets.items():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(content)
    print("shared host files prepared; service not activated")
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as exc:
        print("shared host setup failed: " + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
