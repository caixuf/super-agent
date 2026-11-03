import os
from pathlib import Path
import subprocess
import sys

SCRIPT = Path(__file__).resolve().parents[1] / "tools/hermes_pm_preflight.py"

def test_preflight_syncs_only_stale_environment_with_bound_home(tmp_path):
    root = tmp_path / "hermes"
    home = tmp_path / "profile"
    root.mkdir(); home.mkdir()
    (root / "pm.py").write_text("from pathlib import Path\ndef venv_is_current(project_root): return (Path(project_root) / 'current').exists()\n")
    launcher = root / ".hermes/bin/hermes"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nprintf '%s\n' \"$HERMES_HOME\" > \"$TEST_ROOT/home-seen\"\ntouch \"$TEST_ROOT/current\"\n")
    launcher.chmod(0o700)
    cmd = [sys.executable, str(SCRIPT), "--hermes-root", str(root), "--hermes-home", str(home)]
    env = dict(os.environ, TEST_ROOT=str(root), HERMES_HOME="/wrong")
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (root / "home-seen").read_text().strip() == str(home)
    (root / "home-seen").unlink()
    assert subprocess.run(cmd, env=env, capture_output=True).returncode == 0
    assert not (root / "home-seen").exists()

def test_preflight_refuses_missing_launcher(tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPT), "--hermes-root", str(tmp_path),
                             "--hermes-home", str(tmp_path)], capture_output=True, text=True)
    assert result.returncode != 0
