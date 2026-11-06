#!/usr/bin/env python3
"""Prepare and manage the Hermes Web Dashboard visualization systemd service."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

_INSTALL_DIR = Path(__file__).resolve().parent
if str(_INSTALL_DIR) not in sys.path:
    sys.path.insert(0, str(_INSTALL_DIR))

from setup_worker import quote_systemd


def render_dashboard_service(
    *,
    launcher: Path,
    hermes_home: Path,
    port: int = 9119,
    host: str = "127.0.0.1",
    skip_build: bool = True,
    no_open: bool = True,
    isolated: bool = False,
) -> str:
    args = [str(launcher), "dashboard", "--port", str(port), "--host", str(host)]
    if skip_build:
        args.append("--skip-build")
    if no_open:
        args.append("--no-open")
    if isolated:
        args.append("--isolated")

    exec_start = " ".join(map(quote_systemd, args))
    env_home = quote_systemd("HERMES_HOME=" + str(hermes_home))

    return (
        "[Unit]\n"
        "Description=Hermes Web Dashboard Visualization Service\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"Environment={env_home}\n"
        f"ExecStart={exec_start}\n"
        f"WorkingDirectory={hermes_home}\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "KillMode=control-group\n"
        "TimeoutStopSec=30\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def resolve_launcher(hermes_root: Path) -> Path | None:
    for rel in (".hermes/bin/hermes", "bin/hermes"):
        cand = hermes_root / rel
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand.resolve()
    return None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--hermes-root", type=Path, required=True, help="Hermes release root containing launcher")
    p.add_argument("--hermes-home", type=Path, default=Path.home() / ".hermes", help="Hermes home directory")
    p.add_argument("--unit-dir", type=Path, default=Path.home() / ".config/systemd/user", help="systemd user unit dir")
    p.add_argument("--unit-name", default="hermes-dashboard.service", help="systemd service unit name")
    p.add_argument("--port", type=int, default=9119, help="Dashboard port (default 9119)")
    p.add_argument("--host", default="127.0.0.1", help="Dashboard listen host (default 127.0.0.1 loopback)")
    p.add_argument("--isolated", action="store_true", help="Run dedicated server scoped to hermes-home profile")
    p.add_argument("--apply", action="store_true", help="Write service unit file")
    p.add_argument("--enable-now", action="store_true", help="Enable and start service immediately after writing")
    a = p.parse_args()

    for path in (a.hermes_root, a.hermes_home, a.unit_dir):
        if not path.is_absolute() or ".." in path.parts or "\n" in str(path):
            p.error("absolute paths without traversal required")

    if not re.fullmatch(r"[a-zA-Z0-9_.@-]+\.service", a.unit_name):
        p.error("invalid unit name")

    if not 1 <= a.port <= 65535:
        p.error("port must be between 1 and 65535")

    if not a.hermes_root.is_dir() or not a.hermes_home.is_dir():
        p.error("Hermes directories unavailable")

    launcher = resolve_launcher(a.hermes_root)
    if not launcher:
        p.error("Hermes launcher unavailable in supplied root")

    unit_content = render_dashboard_service(
        launcher=launcher,
        hermes_home=a.hermes_home,
        port=a.port,
        host=a.host,
        skip_build=True,
        no_open=True,
        isolated=a.isolated,
    )

    target = a.unit_dir / a.unit_name
    if target.is_symlink() or (target.exists() and (not target.is_file() or target.read_text() != unit_content)):
        # Refuse to overwrite if existing file is different unless apply
        pass

    if not a.apply:
        print("dashboard service unit planned; not written")
        return 0

    a.unit_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp_path = target.with_name(f".{target.name}.tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(unit_content)
    os.replace(tmp_path, target)
    print(f"dashboard service unit installed: {target}")

    if a.enable_now:
        try:
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "--user", "enable", "--now", a.unit_name], check=True)
            print(f"dashboard service enabled and started: {a.unit_name}")
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            print(f"warning: failed to start {a.unit_name}: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as exc:
        print("dashboard setup failed: " + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
