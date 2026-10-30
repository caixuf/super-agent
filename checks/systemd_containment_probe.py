#!/usr/bin/env python3
"""Opt-in local probe: does this user manager stop a setsid child in its unit?

This is an experiment, not production containment evidence. It starts one
randomly named transient user service and stops only that service.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid


def _run(*args):
    return subprocess.run(args, capture_output=True, text=True, timeout=12)


def _state(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].strip().split()[0]
    except FileNotFoundError:
        return "absent"


def main():
    unit = "agentcore-containment-probe-" + uuid.uuid4().hex + ".service"
    result = {"ok": False, "unit": unit, "scope": "same_systemd_unit_only"}
    with tempfile.TemporaryDirectory(prefix="agentcore-containment-") as directory:
        marker = Path(directory) / "child.pid"
        try:
            started = _run("systemd-run", "--user", "--unit", unit,
                           "--property=Type=exec", "--property=KillMode=control-group",
                           "/bin/sh", "-c", 'setsid sleep 30 & echo $! > "$1"; wait',
                           "sh", str(marker))
            if started.returncode:
                result["reason"] = "unit_start_failed"
                return result
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            if not marker.exists():
                result["reason"] = "child_not_observed"
                return result
            pid = int(marker.read_text())
            query = _run("systemctl", "--user", "show", unit,
                         "--property=ControlGroup", "--value")
            leader = _run("systemctl", "--user", "show", unit,
                          "--property=MainPID", "--value")
            group = query.stdout.strip()
            group_file = Path(f"/proc/{pid}/cgroup")
            child_group = group_file.read_text().strip() if group_file.exists() else ""
            main_pid = int(leader.stdout.strip() or "0")
            if (query.returncode or leader.returncode or not group or not main_pid
                    or child_group != "0::" + group or os.getsid(pid) == os.getsid(main_pid)):
                result["reason"] = "isolation_not_observed"
                return result
            stopped = _run("systemctl", "--user", "stop", unit)
            if stopped.returncode:
                result["reason"] = "unit_stop_failed"
                return result
            deadline = time.monotonic() + 5
            state = _state(pid)
            while state not in ("absent", "Z") and time.monotonic() < deadline:
                time.sleep(.05)
                state = _state(pid)
            result.update(ok=state in ("absent", "Z"), child_state=state,
                          detached_child_in_unit=True)
            if not result["ok"]:
                result["reason"] = "child_survived_stop"
            return result
        except (OSError, ValueError, subprocess.SubprocessError):
            result["reason"] = "probe_unavailable_or_failed"
            return result
        finally:
            try:
                _run("systemctl", "--user", "stop", unit)
                _run("systemctl", "--user", "reset-failed", unit)
            except (OSError, subprocess.SubprocessError):
                pass


if __name__ == "__main__":
    outcome = main()
    print(json.dumps(outcome, ensure_ascii=False))
    sys.exit(0 if outcome["ok"] else 1)
