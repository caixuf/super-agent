"""Tests for hermes-dashboard systemd service generator."""
from __future__ import annotations
from pathlib import Path
import pytest
from install.setup_dashboard import render_dashboard_service, resolve_launcher, main


def test_render_dashboard_service():
    rendered = render_dashboard_service(
        launcher=Path("/opt/hermes/.hermes/bin/hermes"),
        hermes_home=Path("/home/user/.hermes"),
        port=9119,
        host="127.0.0.1",
        skip_build=True,
        no_open=True,
    )
    assert "[Unit]" in rendered
    assert "Description=Hermes Web Dashboard Visualization Service" in rendered
    assert 'ExecStart="/opt/hermes/.hermes/bin/hermes" "dashboard" "--port" "9119" "--host" "127.0.0.1" "--skip-build" "--no-open"' in rendered
    assert 'Environment="HERMES_HOME=/home/user/.hermes"' in rendered
    assert 'WorkingDirectory="/home/user/.hermes"' in rendered
    assert "Restart=always" in rendered


def test_resolve_launcher(tmp_path):
    root = tmp_path / "hermes_root"
    bin_dir = root / ".hermes" / "bin"
    bin_dir.mkdir(parents=True)
    launcher = bin_dir / "hermes"
    launcher.write_text("#!/bin/sh\nexit 0\n")
    launcher.chmod(0o755)

    assert resolve_launcher(root) == launcher.resolve()


def test_main_plan_and_apply(tmp_path, monkeypatch, capsys):
    root = tmp_path / "hermes_root"
    launcher = root / ".hermes" / "bin" / "hermes"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n")
    launcher.chmod(0o755)

    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    unit_dir = tmp_path / "systemd" / "user"

    # 1. Plan mode (without --apply)
    test_args = [
        "setup_dashboard.py",
        "--hermes-root", str(root),
        "--hermes-home", str(hermes_home),
        "--unit-dir", str(unit_dir),
    ]
    monkeypatch.setattr("sys.argv", test_args)
    rc = main()
    assert rc == 0
    captured = capsys.readouterr()
    assert "planned; not written" in captured.out
    assert not (unit_dir / "hermes-dashboard.service").exists()

    # 2. Apply mode
    test_args.append("--apply")
    monkeypatch.setattr("sys.argv", test_args)
    rc = main()
    assert rc == 0
    captured = capsys.readouterr()
    assert "dashboard service unit installed" in captured.out
    installed = unit_dir / "hermes-dashboard.service"
    assert installed.exists()
    content = installed.read_text()
    assert '"--port" "9119"' in content
