import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "install/setup_shared_host.py"

def test_shared_host_install_is_idempotent_and_refuses_drift(tmp_path):
    root, home = tmp_path / "hermes", tmp_path / "home"
    root.mkdir(); home.mkdir()
    launcher = root / ".hermes/bin/hermes"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n")
    launcher.chmod(0o700)
    state, units = tmp_path / "state", tmp_path / "units"
    cmd = [sys.executable, str(SCRIPT), "--hermes-root", str(root), "--hermes-home", str(home),
           "--state-dir", str(state), "--unit-dir", str(units), "--proxy", "http://127.0.0.1:7899"]
    assert subprocess.run(cmd, capture_output=True).returncode == 0
    assert not state.exists() and not units.exists()
    result = subprocess.run(cmd + ["--apply"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    unit = units / "hermes-shared-host.service"
    before = unit.read_text()
    assert "--external-supervisor" in before and "TimeoutStartSec=660" in before
    assert subprocess.run(cmd + ["--apply"], capture_output=True).returncode == 0
    assert unit.read_text() == before
    unit.write_text(before + "# user change\n")
    assert subprocess.run(cmd + ["--apply"], capture_output=True).returncode != 0
    assert unit.read_text() == before + "# user change\n"

def test_shared_host_refuses_proxy_credentials(tmp_path):
    root, home = tmp_path / "h", tmp_path / "p"
    root.mkdir();home.mkdir()
    launcher = root / ".hermes/bin/hermes"
    launcher.parent.mkdir(parents=True);launcher.write_text("#!/bin/sh\n");launcher.chmod(0o700)
    result = subprocess.run([sys.executable, str(SCRIPT), "--hermes-root", str(root), "--hermes-home", str(home),
                             "--state-dir", str(tmp_path / "s"), "--unit-dir", str(tmp_path / "u"),
                             "--proxy", "http://user:password@127.0.0.1:7899", "--apply"], capture_output=True)
    assert result.returncode != 0
    assert not (tmp_path / "s").exists()
