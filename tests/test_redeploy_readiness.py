"""Read-only readiness reports fail closed and never echo profile settings."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK = REPO_ROOT / "tools" / "redeploy_check.py"


def test_missing_profile_fails_closed_in_machine_report(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(CHECK), "--readiness-profile", str(tmp_path), "--json"],
        cwd=REPO_ROOT, text=True, capture_output=True, check=False,
    )
    assert proc.returncode == 1
    report = json.loads(proc.stdout)
    assert report["runtime_ready"] is False
    assert report["state"] == "blocked"
    assert "profile_config_missing_or_invalid" in report["blockers"]
    assert "runtime_tool_registration_unverified" in report["runtime_blockers"]


def test_invalid_config_report_does_not_echo_sensitive_values(tmp_path):
    secret = "private-value-never-print"
    (tmp_path / "config.yaml").write_text(f"token: {secret}\nplugins: [\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(CHECK), "--readiness-profile", str(tmp_path), "--json"],
        cwd=REPO_ROOT, text=True, capture_output=True, check=False,
    )
    assert proc.returncode == 1
    assert secret not in proc.stdout
    assert secret not in proc.stderr
    report = json.loads(proc.stdout)
    assert report["blockers"] == ["profile_config_missing_or_invalid"]
