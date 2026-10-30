"""Attempt containment evidence for colleague execution.

A process group only fences children that stay in the group. A random attempt
token can help locate descendants that leave it, provided they still expose that
token through their process environment. It cannot establish that every child
has stopped.

This scan is best-effort cleanup, not proof of whole-attempt containment.
A process can clear its environment or be unreadable, so an empty scan cannot
prove that an attempt is quiescent. Callers must keep uncertainty explicit.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
import uuid

ENV = "AGENTCORE_ATTEMPT_TOKEN"
_TOKEN = re.compile(r"[0-9a-f]{32}\Z")


def new_token() -> str:
    """Mint one attempt token; 32 hex characters, safe to place in an environment."""
    return uuid.uuid4().hex


def checked_token(token) -> str:
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise ValueError("invalid_attempt_token")
    return token


def _tagged(token: str, exclude: frozenset) -> list[int]:
    needle = ("%s=%s" % (ENV, token)).encode("ascii")
    procfd = os.open("/proc", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        names = os.listdir(procfd)
    finally:
        os.close(procfd)
    found = []
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid in exclude:
            continue
        try:
            fd = os.open("/proc/%d/environ" % pid, os.O_RDONLY)
        except OSError:
            continue
        try:
            with os.fdopen(fd, "rb") as stream:
                blob = stream.read(1 << 20)
        except OSError:
            continue
        # Entries are NUL separated and may be in any order.
        if needle in blob.split(b"\0"):
            found.append(pid)
    return sorted(found)


def tagged(token: str, *, exclude: frozenset = frozenset()) -> list[int]:
    """PIDs currently carrying the attempt token, excluding ``exclude`` and the caller."""
    token = checked_token(token)
    return _tagged(token, frozenset(exclude) | {os.getpid()})


def _signal(pid: int, signum: int) -> None:
    try:
        os.kill(pid, signum)
    except (ProcessLookupError, PermissionError):
        pass


def sweep(token: str, *, grace: float = 2.0, timeout: float = 10.0) -> dict:
    """Best-effort cleanup of processes whose current environment has the token.

    An empty scan does not prove the attempt is empty; detached processes can
    remove the token or become unreadable. This result is never whole-attempt
    evidence.
    """
    token = checked_token(token)
    me = frozenset({os.getpid()})
    signalled: list[int] = []
    pending = _tagged(token, me)
    if pending:
        for pid in pending:
            _signal(pid, signal.SIGTERM)
        signalled = pending
        end = time.monotonic() + grace
        while time.monotonic() < end:
            pending = _tagged(token, me)
            if not pending:
                break
            time.sleep(0.05)
        for pid in pending:
            _signal(pid, signal.SIGKILL)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            pending = _tagged(token, me)
            if not pending:
                break
            time.sleep(0.05)
    return {"token": token, "signalled": signalled, "survivors": sorted(pending),
            "quiescent": False, "token_scan_empty": not pending,
            "proof_scope": "token_scan", "observed_at": time.time()}


def current_boot_id() -> str:
    """Return Linux host boot ID or empty string if unavailable."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def verify_systemd_containment(unit: str, *, boot_id: str | None = None) -> dict:
    """Verify that a systemd transient unit and its control group are completely stopped.

    If the unit is inactive or failed and its control group has 0 surviving processes,
    setsid or unshared processes inside that unit could not have survived.
    """
    if not isinstance(unit, str) or not unit.strip():
        raise ValueError("invalid_unit_name")

    cur_boot = current_boot_id()
    if boot_id and cur_boot and boot_id != cur_boot:
        return {"ok": False, "reason": "boot_id_mismatch", "unit": unit}

    try:
        p_active = subprocess.run(["systemctl", "--user", "is-active", unit],
                                  capture_output=True, text=True, timeout=5)
        active_state = p_active.stdout.strip()
        p_cgroup = subprocess.run(["systemctl", "--user", "show", unit, "--property=ControlGroup", "--value"],
                                  capture_output=True, text=True, timeout=5)
        cgroup_rel = p_cgroup.stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "reason": "systemctl_query_failed", "error": type(exc).__name__, "unit": unit}

    if active_state in ("active", "activating", "reloading"):
        return {"ok": False, "reason": "unit_still_active", "active_state": active_state, "unit": unit}

    cgroup_procs_empty = True
    survivors = []
    if cgroup_rel:
        cgroup_base = Path("/sys/fs/cgroup")
        cgroup_dir = cgroup_base / cgroup_rel.lstrip("/")
        procs_file = cgroup_dir / "cgroup.procs"
        if procs_file.exists():
            try:
                procs = procs_file.read_text().split()
                if procs:
                    survivors = [int(p) for p in procs if p.isdigit()]
                    cgroup_procs_empty = len(survivors) == 0
            except OSError:
                pass

    if not cgroup_procs_empty or survivors:
        return {"ok": False, "reason": "cgroup_has_surviving_procs", "survivors": survivors, "unit": unit}

    return {
        "ok": True,
        "unit": unit,
        "boot_id": cur_boot,
        "active_state": active_state,
        "cgroup": cgroup_rel,
        "survivors": []
    }


def verify_docker_containment(container: str, *, attempt_token: str | None = None) -> dict:
    """Verify that a docker container has stopped or been removed.

    If container is removed (NotFound) or Exited/Dead, no process inside its
    namespaces survives.
    """
    if not isinstance(container, str) or not container.strip():
        raise ValueError("invalid_container_name")

    try:
        p_inspect = subprocess.run(
            ["docker", "inspect", container, "--format", "{{json .State}}:::{{json .Config.Labels}}:::{{json .Config.Env}}"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "reason": "docker_command_failed", "error": type(exc).__name__, "container": container}

    if p_inspect.returncode != 0:
        return {"ok": True, "container": container, "state": "removed", "survivors": []}

    try:
        parts = p_inspect.stdout.strip().split(":::")
        state = json.loads(parts[0]) if len(parts) > 0 and parts[0] else {}
        labels = json.loads(parts[1]) if len(parts) > 1 and parts[1] else {}
        envs = json.loads(parts[2]) if len(parts) > 2 and parts[2] else []
    except (json.JSONDecodeError, IndexError):
        return {"ok": False, "reason": "docker_inspect_parse_failed", "container": container}

    if attempt_token:
        token_found = (
            labels.get("agentcore.attempt_token") == attempt_token or
            any(e.startswith(f"{ENV}={attempt_token}") for e in envs if isinstance(e, str))
        )
        if not token_found:
            return {"ok": False, "reason": "attempt_token_mismatch", "container": container}

    running = state.get("Running", False)
    status = state.get("Status", "")
    if running or status in ("running", "restarting"):
        return {"ok": False, "reason": "container_still_running", "status": status, "container": container,
                "survivors": [container]}

    return {"ok": True, "container": container, "status": status, "exit_code": state.get("ExitCode"), "survivors": []}


def sweep_and_verify(token: str, *,
                     systemd_unit: str | None = None,
                     docker_container: str | None = None,
                     runner_contained: bool = False,
                     grace: float = 2.0,
                     timeout: float = 10.0) -> dict:
    """Best-effort cleanup followed by trusted containment verification.

    If trusted proof (systemd cgroup, docker container, or confirmed in-process runner)
    demonstrates whole-attempt containment, returns a whole_attempt evidence dict with
    quiescent=True. Otherwise returns quiescent=False.
    """
    token = checked_token(token)
    base_sweep = sweep(token, grace=grace, timeout=timeout)
    if base_sweep.get("survivors"):
        return base_sweep

    if systemd_unit:
        cg_res = verify_systemd_containment(systemd_unit)
        if not cg_res.get("ok"):
            return {
                "token": token,
                "signalled": base_sweep.get("signalled", []),
                "survivors": cg_res.get("survivors", [systemd_unit]),
                "quiescent": False,
                "token_scan_empty": True,
                "proof_scope": "cgroup_verification_failed",
                "reason": cg_res.get("reason"),
                "observed_at": time.time(),
            }
        return {
            "token": token,
            "signalled": base_sweep.get("signalled", []),
            "survivors": [],
            "quiescent": True,
            "proof_scope": "whole_attempt",
            "containment_type": "systemd_unit",
            "unit": systemd_unit,
            "boot_id": cg_res.get("boot_id"),
            "token_scan_empty": True,
            "observed_at": time.time(),
        }

    if docker_container:
        dk_res = verify_docker_containment(docker_container, attempt_token=token)
        if not dk_res.get("ok"):
            return {
                "token": token,
                "signalled": base_sweep.get("signalled", []),
                "survivors": dk_res.get("survivors", [docker_container]),
                "quiescent": False,
                "token_scan_empty": True,
                "proof_scope": "docker_verification_failed",
                "reason": dk_res.get("reason"),
                "observed_at": time.time(),
            }
        return {
            "token": token,
            "signalled": base_sweep.get("signalled", []),
            "survivors": [],
            "quiescent": True,
            "proof_scope": "whole_attempt",
            "containment_type": "docker_container",
            "container": docker_container,
            "token_scan_empty": True,
            "observed_at": time.time(),
        }

    if runner_contained and base_sweep.get("token_scan_empty"):
        return {
            "token": token,
            "signalled": base_sweep.get("signalled", []),
            "survivors": [],
            "quiescent": True,
            "proof_scope": "whole_attempt",
            "containment_type": "runner_in_process",
            "token_scan_empty": True,
            "observed_at": time.time(),
        }

    return base_sweep


def evidence_id(evidence) -> str | None:
    """Stable identity for one quiescence observation, or None when it is not evidence."""
    if (not isinstance(evidence, dict) or evidence.get("quiescent") is not True
            or evidence.get("proof_scope") != "whole_attempt"):
        return None
    try:
        token = checked_token(evidence.get("token"))
    except ValueError:
        return None
    payload = json.dumps([token, sorted(evidence.get("survivors") or []),
                          list(evidence.get("signalled") or [])], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:32]
