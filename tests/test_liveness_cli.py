"""`python3 -m agentcore.liveness` — periodic sweep entry point (systemd timer friendly).

It must be usable with a *plain* interpreter and a bare checkout on PYTHONPATH: no
third-party imports, no daemon, no background thread. Sweep writes only rows whose
classification changed and never turns silence into a success.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from agentcore.liveness import LivenessLedger, pid_start_time

ROOT = Path(__file__).resolve().parents[1]


def test_cli_sweeps_stale_rows_and_reports_counts(tmp_path, capsys):
    db = tmp_path / "liveness.db"
    store = LivenessLedger(str(db), stall_seconds=60)
    store.register("gone", kind="terminal", owner="p:s", pid=999999,
                   pid_start="deadbeef:1", state="running")
    me = os.getpid()
    store.register("live", kind="terminal", owner="p:s", pid=me,
                   pid_start=pid_start_time(me), state="running")

    from agentcore.liveness import main

    assert main(["--db", str(db), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [row["task_id"] for row in payload["changed"]] == ["gone"]
    assert payload["changed"][0]["state"] == "lost"
    assert payload["counts"] == {"lost": 1, "running": 1}

    # Second run is a no-op: the classification is already persisted.
    assert main(["--db", str(db), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["changed"] == []


def test_cli_runs_as_a_module_with_a_bare_checkout(tmp_path):
    """A timer runs `python3 -m agentcore.liveness` with only PYTHONPATH set."""
    db = tmp_path / "liveness.db"
    store = LivenessLedger(str(db), stall_seconds=60)
    store.register("gone", kind="terminal", owner="p:s", pid=999999,
                   pid_start="deadbeef:1", state="running")

    env = dict(os.environ, PYTHONPATH=str(ROOT))
    env.pop("PYTHONHOME", None)
    result = subprocess.run([sys.executable, "-m", "agentcore.liveness",
                             "--db", str(db), "--json"],
                            cwd=str(tmp_path), env=env, capture_output=True, text=True,
                            timeout=60)
    assert result.returncode == 0, result.stderr[-2000:]
    payload = json.loads(result.stdout)
    assert payload["changed"][0]["task_id"] == "gone"
    assert LivenessLedger(str(db)).get("gone")["state"] == "lost"