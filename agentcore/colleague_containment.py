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

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Mapping, Optional, Sequence
import uuid
import zlib

ENV = "AGENTCORE_ATTEMPT_TOKEN"
_TOKEN = re.compile(r"[0-9a-f]{32}\Z")

FORBIDDEN_PAYLOAD_BINDING_KEYS: frozenset[str] = frozenset({
    "attempt_binding",
    "binding_path",
    "supervisor_nonce",
    "containment_token",
    "instance_id",
    "trusted_containment",
    "execution_backend_adapter",
    "supervisor_pid",
    "supervisor_start",
    "boot_id",
})

_HOME_CREDENTIAL_NAMES: tuple[str, ...] = (
    ".ssh",
    ".config",
    ".hermes",
    ".gnupg",
    ".aws",
    ".kube",
    ".docker",
    ".netrc",
    ".git-credentials",
    "super_agent",
)

_SYSTEM_FORBIDDEN_PATHS: tuple[str, ...] = (
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/run/user",
    "/run/dbus",
    "/run/systemd",
    "/etc/shadow",
    "/etc/sudoers",
    "/etc/gshadow",
)

_SENSITIVE_ENV_PREFIXES: tuple[str, ...] = (
    "FEISHU_",
    "SSH_",
    "AWS_",
    "OPENAI_",
    "ANTHROPIC_",
    "GITHUB_TOKEN",
    "GITLAB_TOKEN",
    "SUPERAGENT_TEST_HOST_SECRET",
)

DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = (
    "read",
    "write",
    "edit",
    "bash",
    "pytest",
    "unittest",
    "python",
    "git",
)

#: Canonical roles granted host execution privilege (YOLO / exempt from bwrap sandbox)
#: for system maintenance, profile management, and daemon unit operations.
SANDBOX_EXEMPT_ROLES: frozenset[str] = frozenset({"maintainer", "ops"})


def is_role_sandbox_exempt(role: Optional[str]) -> bool:
    """Return True if the role is granted host execution privilege (exempt from bwrap sandbox)."""
    if not role:
        return False
    r = str(role).strip().lower()
    exempt = set(SANDBOX_EXEMPT_ROLES)
    custom = (os.environ.get("SUPERAGENT_SANDBOX_EXEMPT_ROLES") or "").strip()
    if custom:
        exempt.update(x.strip().lower() for x in custom.split(",") if x.strip())
    return r in exempt


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
    """Observe a unit, without claiming that it contains a particular attempt.

    A unit name alone does not bind a Gateway-launched turn or its descendants
    to that cgroup. Missing units, inaccessible cgroups and failed queries are
    unknown, not evidence of quiescence.
    """
    if not isinstance(unit, str) or not unit.strip():
        raise ValueError("invalid_unit_name")
    cur_boot = current_boot_id()
    if not cur_boot or (boot_id and boot_id != cur_boot):
        return {"ok": False, "reason": "boot_id_unavailable_or_mismatch", "unit": unit}
    try:
        active = subprocess.run(["systemctl", "--user", "is-active", unit],
                                capture_output=True, text=True, timeout=5)
        group = subprocess.run(["systemctl", "--user", "show", unit, "--property=ControlGroup", "--value"],
                               capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return {"ok": False, "reason": "systemctl_query_failed", "unit": unit}
    state = active.stdout.strip()
    if state in ("active", "activating", "reloading"):
        return {"ok": False, "reason": "unit_still_active", "active_state": state, "unit": unit}
    if state not in ("inactive", "failed") or group.returncode != 0:
        return {"ok": False, "reason": "unit_state_unknown", "unit": unit}
    relative = group.stdout.strip()
    if not relative.startswith("/") or ".." in Path(relative).parts:
        return {"ok": False, "reason": "cgroup_unavailable", "unit": unit}
    directory = Path("/sys/fs/cgroup") / relative.lstrip("/")
    try:
        # cgroup.events is recursive; an empty parent cgroup.procs alone does
        # not exclude processes in children.
        events = dict(line.split() for line in (directory / "cgroup.events").read_text().splitlines())
    except (OSError, ValueError):
        return {"ok": False, "reason": "cgroup_unavailable", "unit": unit}
    if events.get("populated") != "0":
        return {"ok": False, "reason": "cgroup_populated_or_unknown", "unit": unit}
    return {"ok": False, "reason": "attempt_cgroup_binding_unproven", "unit": unit,
            "active_state": state, "cgroup": relative, "boot_id": cur_boot}


def verify_docker_containment(container: str, *, attempt_token: str | None = None) -> dict:
    """Observe a container; no attempt binding or host side effects are proved."""
    if not isinstance(container, str) or not container.strip():
        raise ValueError("invalid_container_name")

    try:
        p_inspect = subprocess.run(
            ["docker", "inspect", container, "--format", "{{json .State}}:::{{json .Config.Labels}}:::{{json .Config.Env}}"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "reason": "docker_command_failed", "error": type(exc).__name__, "container": container}

    if p_inspect.returncode != 0:
        return {"ok": False, "reason": "docker_inspect_failed_or_missing", "container": container}

    try:
        parts = p_inspect.stdout.strip().split(":::")
        state = json.loads(parts[0]) if len(parts) > 0 and parts[0] else {}
        labels = json.loads(parts[1]) if len(parts) > 1 and parts[1] else {}
        envs = json.loads(parts[2]) if len(parts) > 2 and parts[2] else []
    except (json.JSONDecodeError, IndexError):
        return {"ok": False, "reason": "docker_inspect_parse_failed", "container": container}

    if not isinstance(state, dict) or not isinstance(labels, dict) or not isinstance(envs, list):
        return {"ok": False, "reason": "docker_inspect_parse_failed", "container": container}
    if attempt_token:
        token_found = (
            labels.get("agentcore.attempt_token") == attempt_token or
            any(e == f"{ENV}={attempt_token}" for e in envs if isinstance(e, str))
        )
        if not token_found:
            return {"ok": False, "reason": "attempt_token_mismatch", "container": container}

    running = state.get("Running")
    status = state.get("Status", "")
    if running is True or status in ("running", "restarting", "paused"):
        return {"ok": False, "reason": "container_still_running", "status": status, "container": container,
                "survivors": [container]}

    if running is not False or status not in ("exited", "dead"):
        return {"ok": False, "reason": "container_state_unknown", "container": container}
    return {"ok": False, "reason": "attempt_container_binding_unproven", "container": container,
            "status": status, "exit_code": state.get("ExitCode")}


def verify_subreaper_containment(proof: dict, *,
                                 expected_token: str | None = None,
                                 expected_job: str | None = None,
                                 expected_attempt: str | None = None,
                                 expected_nonce: str | None = None,
                                 expected_supervisor_pid: int | None = None) -> dict:
    """Verify quiescence proof emitted by attempt_supervisor or trusted subreaper.

    Validates that:
    1. The proof dictionary is valid and scoped to subreaper_tree.
    2. Quiescence is proven (all child processes reaped, unquiesced_pids is empty).
    3. Job, attempt, nonce, supervisor_pid, exit_code are valid and bound.
    4. Match expected values if specified.
    """
    if not isinstance(proof, dict):
        return {"ok": False, "reason": "invalid_proof_format", "quiescent": False}
    if proof.get("proof_scope") != "subreaper_tree":
        return {"ok": False, "reason": "invalid_proof_scope", "quiescent": False}
    if proof.get("quiescent") is not True:
        return {"ok": False, "reason": "attempt_not_quiescent", "quiescent": False,
                "unquiesced_pids": proof.get("unquiesced_pids", [])}
    if proof.get("unquiesced_pids"):
        return {"ok": False, "reason": "unquiesced_descendants_remain", "quiescent": False,
                "unquiesced_pids": proof.get("unquiesced_pids")}

    token = proof.get("token")
    try:
        checked_token(token)
    except ValueError:
        return {"ok": False, "reason": "invalid_proof_token", "quiescent": False}

    job = proof.get("job")
    if not isinstance(job, str) or not job.strip():
        return {"ok": False, "reason": "missing_or_invalid_proof_job", "quiescent": False}

    attempt = proof.get("attempt")
    if not isinstance(attempt, str) or not attempt.strip():
        return {"ok": False, "reason": "missing_or_invalid_proof_attempt", "quiescent": False}

    nonce = proof.get("nonce")
    if not isinstance(nonce, str) or not nonce.strip():
        return {"ok": False, "reason": "missing_or_invalid_proof_nonce", "quiescent": False}

    spid = proof.get("supervisor_pid")
    if type(spid) is not int or spid <= 0:
        return {"ok": False, "reason": "invalid_supervisor_pid", "quiescent": False}

    exit_code = proof.get("exit_code")
    if type(exit_code) is not int:
        return {"ok": False, "reason": "invalid_proof_exit_code", "quiescent": False}

    if expected_token and token != expected_token:
        return {"ok": False, "reason": "proof_token_mismatch", "quiescent": False}
    if expected_job and job != expected_job:
        return {"ok": False, "reason": "proof_job_mismatch", "quiescent": False}
    if expected_attempt and attempt != expected_attempt:
        return {"ok": False, "reason": "proof_attempt_mismatch", "quiescent": False}
    if expected_nonce and nonce != expected_nonce:
        return {"ok": False, "reason": "proof_nonce_mismatch", "quiescent": False}
    if expected_supervisor_pid is not None and spid != expected_supervisor_pid:
        return {"ok": False, "reason": "proof_supervisor_pid_mismatch", "quiescent": False}

    evidence = {
        "ok": True,
        "token": token,
        "job": job,
        "attempt": attempt,
        "nonce": nonce,
        "supervisor_pid": spid,
        "quiescent": True,
        "proof_scope": "subreaper_tree",
        "observed_at": time.time(),
        "exit_code": exit_code,
    }
    eid = evidence_id(evidence)
    if eid is None:
        return {"ok": False, "reason": "cannot_compute_evidence_id", "quiescent": False}
    evidence["evidence_id"] = eid
    return evidence


def sweep_and_verify(token: str, *,
                     systemd_unit: str | None = None,
                     docker_container: str | None = None,
                     runner_contained: bool = False,
                     supervisor_proof: dict | None = None,
                     expected_job: str | None = None,
                     expected_attempt: str | None = None,
                     expected_nonce: str | None = None,
                     expected_supervisor_pid: int | None = None,
                     grace: float = 2.0,
                     timeout: float = 10.0) -> dict:
    """Best-effort cleanup; verifies attempt containment if a valid proof or adapter is provided."""
    token = checked_token(token)
    if supervisor_proof is not None:
        # All expected identities must be bound when verifying a supervisor proof
        # to prevent stale stop receipts or reused names from passing.
        if not (expected_job and expected_attempt and expected_nonce):
            return {"ok": False, "reason": "missing_expected_identities", "quiescent": False}
        return verify_subreaper_containment(
            supervisor_proof,
            expected_token=token,
            expected_job=expected_job,
            expected_attempt=expected_attempt,
            expected_nonce=expected_nonce,
            expected_supervisor_pid=expected_supervisor_pid,
        )

    base_sweep = sweep(token, grace=grace, timeout=timeout)
    if base_sweep.get("survivors"):
        return base_sweep

    if systemd_unit or docker_container:
        observation = (verify_systemd_containment(systemd_unit) if systemd_unit
                       else verify_docker_containment(docker_container, attempt_token=token))
        return {
            "token": token, "signalled": base_sweep.get("signalled", []),
            "survivors": observation.get("survivors", []), "quiescent": False,
            "token_scan_empty": True, "proof_scope": "containment_unproven",
            "reason": observation.get("reason", "attempt_binding_unproven"),
            "observed_at": time.time(),
        }

    # A caller-provided boolean and an empty environment scan do not prove
    # that a turn and all its descendants were confined or have stopped.
    return base_sweep


def evidence_id(evidence) -> str | None:
    """Stable identity for one quiescence observation, or None when it is not evidence."""
    if not isinstance(evidence, dict) or evidence.get("quiescent") is not True:
        return None
    scope = evidence.get("proof_scope")
    if scope not in ("whole_attempt", "subreaper_tree"):
        return None
    try:
        token = checked_token(evidence.get("token"))
    except ValueError:
        return None
    if scope == "whole_attempt":
        payload = json.dumps([token, sorted(evidence.get("survivors") or []),
                              list(evidence.get("signalled") or [])], separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()[:32]
    elif scope == "subreaper_tree":
        if evidence.get("unquiesced_pids") or evidence.get("survivors"):
            return None
        job = evidence.get("job")
        attempt = evidence.get("attempt")
        nonce = evidence.get("nonce")
        spid = evidence.get("supervisor_pid")
        if (not isinstance(job, str) or not job.strip()
                or not isinstance(attempt, str) or not attempt.strip()
                or not isinstance(nonce, str) or not nonce.strip()
                or type(spid) is not int or spid <= 0):
            return None
        payload = json.dumps([token, job, attempt, nonce, spid],
                             separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()[:32]
    return None


def process_start(pid: int) -> str | None:
    """Return boot_id:starttime for a live Linux PID, or None if absent/unreadable."""
    if type(pid) is not int or pid <= 0:
        return None
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        boot = current_boot_id()
        if not boot:
            return None
        return f"{boot}:{fields[19]}"
    except (OSError, IndexError):
        return None


def _descendant_pids(root_pid: int) -> list[int]:
    """Recursively collect current child/descendant PIDs from /proc/<pid>/task/*/children."""
    seen: set[int] = set()
    queue = [root_pid]
    while queue:
        cur = queue.pop(0)
        try:
            task_dir = Path(f"/proc/{cur}/task")
            if not task_dir.is_dir():
                continue
            for tid_dir in task_dir.iterdir():
                cfile = tid_dir / "children"
                if not cfile.is_file():
                    continue
                for tok in cfile.read_text().split():
                    if tok.isdigit():
                        cpid = int(tok)
                        if cpid not in seen and cpid != root_pid:
                            seen.add(cpid)
                            queue.append(cpid)
        except OSError:
            continue
    return sorted(seen)


def is_forbidden_host_path(path: str | Path) -> tuple[bool, str]:
    """Check whether a path targets high-privilege host credentials or control sockets."""
    raw = Path(path).expanduser()
    try:
        resolved = raw.resolve(strict=False)
    except OSError:
        resolved = raw
    home = Path.home().resolve(strict=False)
    for cred_name in _HOME_CREDENTIAL_NAMES:
        cred_path = home / cred_name
        if resolved == cred_path or resolved.is_relative_to(cred_path):
            return True, f"forbidden_host_credential_path:{cred_name}"
        if raw == cred_path or (raw.is_absolute() and raw.is_relative_to(cred_path)):
            return True, f"forbidden_host_credential_path:{cred_name}"
    for sys_forbid in _SYSTEM_FORBIDDEN_PATHS:
        sys_path = Path(sys_forbid)
        if resolved == sys_path or resolved.is_relative_to(sys_path):
            return True, f"forbidden_control_socket_or_system_path:{sys_forbid}"
        if raw == sys_path or (raw.is_absolute() and raw.is_relative_to(sys_path)):
            return True, f"forbidden_control_socket_or_system_path:{sys_forbid}"
    return False, ""


@dataclass(frozen=True)
class ResourceBudgets:
    """Enforced execution budgets for an attempt-bound sandbox."""
    timeout_s: float = 60.0
    max_procs: int = 32
    max_memory_bytes: int = 512 * 1024 * 1024
    max_output_bytes: int = 1024 * 1024
    max_file_bytes: int = 64 * 1024 * 1024
    max_disk_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        if (isinstance(self.timeout_s, bool) or not isinstance(self.timeout_s, (int, float))
                or not math.isfinite(self.timeout_s) or self.timeout_s <= 0 or self.timeout_s > 900.0):
            raise ValueError("invalid_resource_budget:timeout_s")
        for field_name, low, high in (
            ("max_procs", 1, 512),
            ("max_memory_bytes", 16 * 1024 * 1024, 8 * 1024 * 1024 * 1024),
            ("max_output_bytes", 1024, 16 * 1024 * 1024),
            ("max_file_bytes", 1024, 512 * 1024 * 1024),
            ("max_disk_bytes", 4096, 1024 * 1024 * 1024),
        ):
            val = getattr(self, field_name)
            if type(val) is not int or not (low <= val <= high):
                raise ValueError(f"invalid_resource_budget:{field_name}")


def normalize_resource_budgets(raw: ResourceBudgets | Mapping[str, Any] | None = None) -> ResourceBudgets:
    if raw is None:
        return ResourceBudgets()
    if isinstance(raw, ResourceBudgets):
        return raw
    if not isinstance(raw, Mapping):
        raise ValueError("invalid_resource_budget:not_a_mapping")
    allowed_keys = {"timeout_s", "max_procs", "max_memory_bytes", "max_output_bytes", "max_file_bytes", "max_disk_bytes"}
    if set(raw) - allowed_keys:
        raise ValueError("invalid_resource_budget:unknown_keys")
    kwargs: dict[str, Any] = {}
    for k in allowed_keys:
        if k in raw:
            kwargs[k] = raw[k]
    return ResourceBudgets(**kwargs)


def _read_private_binding_file(binding_path: str | Path) -> dict[str, Any]:
    """Read a host-created private binding file without following symlinks."""
    path = Path(binding_path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode)
                or not (0 < info.st_size <= 8192)
                or (info.st_mode & 0o077) != 0
                or info.st_nlink != 1
                or (hasattr(os, "getuid") and info.st_uid != os.getuid())):
            raise PermissionError("untrusted_or_insecure_binding_file")
        raw = os.read(fd, 8193)
    finally:
        os.close(fd)
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("invalid_binding_json")
    return data


def verify_attempt_binding_record(
    binding: Mapping[str, Any],
    *,
    expected_job: str | None = None,
    expected_attempt: str | None = None,
    expected_nonce: str | None = None,
    expected_token: str | None = None,
    expected_supervisor_pid: int | None = None,
    expected_instance_id: str | None = None,
    expected_generation: int | None = None,
    check_live_supervisor: bool = True,
) -> dict[str, Any]:
    """Validate a pre-execution attempt binding created by the trusted host.

    Explicitly rejects post-execution stop proofs (proof_scope == 'subreaper_tree'
    or quiescent present) and requires expected identities from the host/pool.
    """
    if not isinstance(binding, Mapping):
        return {"ok": False, "reason": "invalid_binding_format"}
    if binding.get("proof_scope") == "subreaper_tree" or "quiescent" in binding:
        return {"ok": False, "reason": "stop_proof_cannot_open_pre_execution_gate"}
    if binding.get("proof_type") != "attempt_pre_execution_binding":
        return {"ok": False, "reason": "invalid_binding_proof_type"}
    if binding.get("backend") != "bwrap_subreaper":
        return {"ok": False, "reason": "unsupported_binding_backend"}

    if not (expected_job and expected_attempt and expected_nonce and expected_token):
        return {"ok": False, "reason": "missing_expected_binding_identities"}

    try:
        token = checked_token(binding.get("token"))
        exp_token = checked_token(expected_token)
    except ValueError:
        return {"ok": False, "reason": "invalid_binding_token"}

    job = binding.get("job")
    attempt = binding.get("attempt")
    nonce = binding.get("nonce")
    instance_id = binding.get("instance_id")
    generation = binding.get("generation")
    boot_id = binding.get("boot_id")
    spid = binding.get("supervisor_pid")
    sstart = binding.get("supervisor_start")

    if not isinstance(job, str) or not job.strip():
        return {"ok": False, "reason": "missing_or_invalid_binding_job"}
    if not isinstance(attempt, str) or not attempt.strip():
        return {"ok": False, "reason": "missing_or_invalid_binding_attempt"}
    if not isinstance(nonce, str) or not nonce.strip():
        return {"ok": False, "reason": "missing_or_invalid_binding_nonce"}
    if not isinstance(instance_id, str) or not _TOKEN.fullmatch(instance_id):
        return {"ok": False, "reason": "missing_or_invalid_instance_id"}
    if type(generation) is not int or generation < 1:
        return {"ok": False, "reason": "missing_or_invalid_generation"}
    if not isinstance(boot_id, str) or not boot_id.strip():
        return {"ok": False, "reason": "missing_or_invalid_boot_id"}
    if type(spid) is not int or spid <= 0:
        return {"ok": False, "reason": "invalid_binding_supervisor_pid"}
    if not isinstance(sstart, str) or not sstart.strip():
        return {"ok": False, "reason": "missing_or_invalid_supervisor_start"}

    if job != expected_job:
        return {"ok": False, "reason": "binding_job_mismatch"}
    if attempt != expected_attempt:
        return {"ok": False, "reason": "binding_attempt_mismatch"}
    if nonce != expected_nonce:
        return {"ok": False, "reason": "binding_nonce_mismatch"}
    if token != exp_token:
        return {"ok": False, "reason": "binding_token_mismatch"}
    if expected_supervisor_pid is not None and spid != expected_supervisor_pid:
        return {"ok": False, "reason": "binding_supervisor_pid_mismatch"}
    if expected_instance_id is not None and instance_id != expected_instance_id:
        return {"ok": False, "reason": "binding_instance_id_mismatch"}
    if expected_generation is not None and generation != expected_generation:
        return {"ok": False, "reason": "binding_generation_mismatch"}

    cur_boot = current_boot_id()
    if not cur_boot or boot_id != cur_boot:
        return {"ok": False, "reason": "binding_boot_id_mismatch"}

    if check_live_supervisor:
        live_start = process_start(spid)
        if not live_start or live_start != sstart:
            return {"ok": False, "reason": "supervisor_not_alive_or_reused"}

    bwrap_bin = shutil.which("bwrap")
    if not bwrap_bin:
        return {"ok": False, "reason": "bwrap_backend_unavailable"}

    return {
        "ok": True,
        "proof_type": "attempt_pre_execution_binding",
        "backend": "bwrap_subreaper",
        "job": job,
        "attempt": attempt,
        "token": token,
        "nonce": nonce,
        "instance_id": instance_id,
        "generation": generation,
        "boot_id": boot_id,
        "supervisor_pid": spid,
        "supervisor_start": sstart,
        "bwrap_path": bwrap_bin,
    }


_MAX_SNAPSHOT_INLINE_BYTES = 16 * 1024 * 1024


def _snapshot_workdir(workdir: Path) -> dict[str, dict[str, Any]]:
    """Capture sha256, size, symlink state, and compressed bytes of files and symlinks in workdir."""
    snap: dict[str, dict[str, Any]] = {}
    if not workdir.is_dir():
        return snap
    for p in sorted(workdir.rglob("*")):
        try:
            rel = p.relative_to(workdir).as_posix()
        except ValueError:
            continue
        if rel == ".git" or rel.startswith(".git/"):
            continue
        if p.is_symlink():
            try:
                link_target = os.readlink(p)
            except OSError:
                continue
            target_bytes = link_target.encode("utf-8", errors="surrogateescape")
            snap[rel] = {
                "is_symlink": True,
                "link_target": link_target,
                "sha256": "symlink:" + hashlib.sha256(target_bytes).hexdigest(),
                "size": len(target_bytes),
                "compressed": None,
            }
            continue
        if not p.is_file():
            continue
        try:
            st = p.stat()
            if st.st_size <= _MAX_SNAPSHOT_INLINE_BYTES:
                raw = p.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                comp = zlib.compress(raw, level=1)
                size = len(raw)
            else:
                h = hashlib.sha256()
                size = 0
                with p.open("rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        h.update(chunk)
                        size += len(chunk)
                digest = h.hexdigest()
                comp = None
        except OSError:
            continue
        snap[rel] = {
            "is_symlink": False,
            "link_target": None,
            "sha256": digest,
            "size": size,
            "compressed": comp,
        }
    return snap


def _file_prefix_sha256(path: Path, prefix_len: int) -> Optional[str]:
    """Compute sha256 of the first prefix_len bytes of path without loading all of it into memory."""
    if prefix_len < 0 or path.is_symlink() or not path.is_file():
        return None
    h = hashlib.sha256()
    remaining = prefix_len
    try:
        with path.open("rb") as f:
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    return None
                h.update(chunk)
                remaining -= len(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _unlink_entry(path: Path) -> None:
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
    except OSError:
        pass


def _restore_snapshot_entry(workdir: Path, rel: str, entry: Mapping[str, Any]) -> None:
    """Restore a single file or symlink in workdir from its pre-execution snapshot entry."""
    dest = workdir / rel
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _unlink_entry(dest)
        if entry.get("is_symlink"):
            link_target = entry.get("link_target")
            if isinstance(link_target, str):
                os.symlink(link_target, dest)
            return
        comp = entry.get("compressed")
        if comp is not None:
            dest.write_bytes(zlib.decompress(comp))
            return
        if (workdir / ".git").exists():
            subprocess.run(
                ["git", "checkout", "--", rel],
                cwd=workdir,
                capture_output=True,
                timeout=5,
                check=False,
            )
    except OSError:
        pass


class AttemptBoundSandboxAdapter:
    """M1 Trusted Execution Backend Adapter combining Linux subreaper + bubblewrap (bwrap).

    Provides:
    - Host-minted persistent binding (job, attempt, token, nonce, instance_id, generation,
      boot_id, supervisor_pid, supervisor_start) verified before execution.
    - Pre-execution role path access enforcement via validate_role_path_access,
      credential/socket path blocking, tool whitelist, and network policy enforcement.
    - Kernel-isolated execution via bubblewrap (--unshare-all --unshare-user --disable-userns
      --die-with-parent --new-session --clearenv) with read-only system mounts, masked
      host credentials/sockets, and role 'never' paths mounted read-only.
    - Enforced resource budgets: timeout, RLIMIT_NPROC (per-userns), RLIMIT_AS,
      RLIMIT_FSIZE, streaming stdout/stderr cap, and tmpfs/workdir disk quota.
    - Deterministic two-phase cancel: blocks new actions first, then terminates and reaps
      all descendants via PR_SET_CHILD_SUBREAPER before emitting stop evidence.
    """

    def __init__(
        self,
        *,
        job: str,
        attempt: str,
        token: str,
        nonce: str,
        instance_id: str,
        generation: int,
        boot_id: str,
        supervisor_pid: int,
        supervisor_start: str,
        binding_path: Optional[Path] = None,
        allowed_workdir: Optional[Path] = None,
        role: str = "cases",
        network_policy: str = "isolated",
        allowed_tools: Optional[Sequence[str]] = None,
        budgets: Optional[ResourceBudgets | Mapping[str, Any]] = None,
        extra_ro_paths: Sequence[str | Path] = (),
    ) -> None:
        self.job = str(job)
        self.attempt = str(attempt)
        self.token = checked_token(token)
        self.nonce = str(nonce)
        if not isinstance(instance_id, str) or not _TOKEN.fullmatch(instance_id):
            raise ValueError("invalid_instance_id")
        self.instance_id = instance_id
        if type(generation) is not int or generation < 1:
            raise ValueError("invalid_generation")
        self.generation = generation
        self.boot_id = str(boot_id)
        self.supervisor_pid = int(supervisor_pid)
        self.supervisor_start = str(supervisor_start)
        self.binding_path = Path(binding_path) if binding_path is not None else None

        if allowed_workdir is not None:
            wpath = Path(allowed_workdir)
            if wpath.is_symlink():
                raise ValueError("workdir_symlink_forbidden")
            resolved_w = wpath.resolve(strict=True)
            if not resolved_w.is_dir():
                raise ValueError("workdir_missing")
            forbidden, reason = is_forbidden_host_path(resolved_w)
            if forbidden:
                raise PermissionError(f"workdir_in_forbidden_path:{reason}")
            self.allowed_workdir: Optional[Path] = resolved_w
        else:
            self.allowed_workdir = None

        from agentcore.colleague_dev import DEV_ROLES
        if role not in DEV_ROLES and not is_role_sandbox_exempt(role):
            raise ValueError(f"unknown_role:{role}")
        self.role = role

        if network_policy not in ("isolated", "allowed"):
            raise ValueError(f"invalid_network_policy:{network_policy}")
        self.network_policy = network_policy

        self.allowed_tools = tuple(allowed_tools) if allowed_tools is not None else DEFAULT_ALLOWED_TOOLS
        if not self.allowed_tools or any(not isinstance(t, str) or not t.strip() for t in self.allowed_tools):
            raise ValueError("invalid_allowed_tools")

        self.budgets = normalize_resource_budgets(budgets)

        ro_list: list[Path] = []
        for p in extra_ro_paths:
            rp = Path(p).resolve(strict=True)
            forbidden, reason = is_forbidden_host_path(rp)
            if forbidden:
                raise PermissionError(f"extra_ro_path_forbidden:{reason}")
            ro_list.append(rp)
        self.extra_ro_paths = tuple(ro_list)

        self.audit_log: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        self._cancelled = False
        self._stopped = False
        self._active_executions = 0
        self._active_control: Optional[socket.socket] = None
        self._active_subreaper_pid: Optional[int] = None
        self._active_bwrap_pid: Optional[int] = None
        self._observed_sandbox_pids: set[int] = set()
        self._last_stop_proof: Optional[dict[str, Any]] = None

    @classmethod
    def create_host_bound(
        cls,
        attempt_dir: str | Path,
        *,
        job: str,
        attempt: str,
        token: Optional[str] = None,
        nonce: Optional[str] = None,
        allowed_workdir: Optional[str | Path] = None,
        workdir: Optional[str | Path] = None,
        role: str = "cases",
        instance_id: Optional[str] = None,
        generation: int = 1,
        supervisor_pid: Optional[int] = None,
        network_policy: str = "isolated",
        allowed_tools: Optional[Sequence[str]] = None,
        budgets: Optional[ResourceBudgets | Mapping[str, Any]] = None,
        extra_ro_paths: Sequence[str | Path] = (),
    ) -> "AttemptBoundSandboxAdapter":
        """Create an attempt-exclusive directory and immutable host binding file."""
        if allowed_workdir is None and workdir is not None:
            allowed_workdir = workdir
        adir = Path(attempt_dir).resolve()
        adir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(adir, 0o700)
        tok = checked_token(token or new_token())
        non = str(nonce or uuid.uuid4().hex)
        inst = str(instance_id or uuid.uuid4().hex)
        if type(generation) is not int or generation < 1:
            raise ValueError("invalid_generation")
        spid = int(supervisor_pid if supervisor_pid is not None else os.getpid())
        sstart = process_start(spid)
        if not sstart:
            raise RuntimeError("supervisor_identity_unavailable")
        boot = sstart.split(":", 1)[0]
        norm_budgets = normalize_resource_budgets(budgets)

        binding_path = adir / "binding.json"
        if binding_path.exists() or binding_path.is_symlink():
            raise FileExistsError("attempt_binding_already_exists")

        binding_data = {
            "schema_version": "attempt_binding/v1",
            "proof_type": "attempt_pre_execution_binding",
            "backend": "bwrap_subreaper",
            "job": str(job),
            "attempt": str(attempt),
            "token": tok,
            "nonce": non,
            "instance_id": inst,
            "generation": generation,
            "boot_id": boot,
            "supervisor_pid": spid,
            "supervisor_start": sstart,
            "role": role,
            "network_policy": network_policy,
            "allowed_workdir": str(Path(allowed_workdir).resolve()) if allowed_workdir is not None else None,
            "budgets": asdict(norm_budgets),
            "created_at": time.time(),
        }
        blob = json.dumps(binding_data, sort_keys=True).encode("utf-8")
        fd = os.open(binding_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            written = 0
            while written < len(blob):
                written += os.write(fd, blob[written:])
            os.fsync(fd)
        finally:
            os.close(fd)

        return cls(
            job=str(job),
            attempt=str(attempt),
            token=tok,
            nonce=non,
            instance_id=inst,
            generation=generation,
            boot_id=boot,
            supervisor_pid=spid,
            supervisor_start=sstart,
            binding_path=binding_path,
            allowed_workdir=Path(allowed_workdir) if allowed_workdir is not None else None,
            role=role,
            network_policy=network_policy,
            allowed_tools=allowed_tools,
            budgets=norm_budgets,
            extra_ro_paths=extra_ro_paths,
        )

    @classmethod
    def from_binding_file(
        cls,
        binding_path: str | Path,
        *,
        allowed_workdir: Optional[str | Path] = None,
        workdir: Optional[str | Path] = None,
        role: Optional[str] = None,
        network_policy: Optional[str] = None,
        allowed_tools: Optional[Sequence[str]] = None,
        budgets: Optional[ResourceBudgets | Mapping[str, Any]] = None,
        extra_ro_paths: Sequence[str | Path] = (),
    ) -> "AttemptBoundSandboxAdapter":
        """Load an adapter from a host-created private binding.json file."""
        if allowed_workdir is None and workdir is not None:
            allowed_workdir = workdir
        data = _read_binding_dict = _read_private_binding_file(binding_path)
        if data.get("proof_type") != "attempt_pre_execution_binding":
            raise PermissionError("invalid_binding_proof_type")
        wdir = allowed_workdir if allowed_workdir is not None else data.get("allowed_workdir")
        resolved_role = role or data.get("role") or "cases"
        resolved_net = network_policy or data.get("network_policy") or "isolated"
        resolved_budgets = budgets if budgets is not None else data.get("budgets")
        return cls(
            job=str(data.get("job") or ""),
            attempt=str(data.get("attempt") or ""),
            token=str(data.get("token") or ""),
            nonce=str(data.get("nonce") or ""),
            instance_id=str(data.get("instance_id") or ""),
            generation=data.get("generation"),
            boot_id=str(data.get("boot_id") or ""),
            supervisor_pid=data.get("supervisor_pid"),
            supervisor_start=str(data.get("supervisor_start") or ""),
            binding_path=Path(binding_path),
            allowed_workdir=Path(wdir) if wdir is not None else None,
            role=resolved_role,
            network_policy=resolved_net,
            allowed_tools=allowed_tools,
            budgets=resolved_budgets,
            extra_ro_paths=extra_ro_paths,
        )

    @classmethod
    def from_environment(
        cls,
        *,
        allowed_workdir: Optional[str | Path] = None,
        workdir: Optional[str | Path] = None,
        role: Optional[str] = None,
        network_policy: Optional[str] = None,
        allowed_tools: Optional[Sequence[str]] = None,
        budgets: Optional[ResourceBudgets | Mapping[str, Any]] = None,
        extra_ro_paths: Sequence[str | Path] = (),
    ) -> "AttemptBoundSandboxAdapter":
        """Construct adapter from SUPERAGENT_ATTEMPT_BINDING_PATH set by attempt_supervisor."""
        if allowed_workdir is None and workdir is not None:
            allowed_workdir = workdir
        bpath = os.environ.get("SUPERAGENT_ATTEMPT_BINDING_PATH", "").strip()
        if not bpath:
            raise RuntimeError("attempt_binding_path_missing")
        return cls.from_binding_file(
            bpath,
            allowed_workdir=allowed_workdir,
            role=role,
            network_policy=network_policy,
            allowed_tools=allowed_tools,
            budgets=budgets,
            extra_ro_paths=extra_ro_paths,
        )

    def verify_attempt_binding(
        self,
        *,
        expected_job: str | None = None,
        expected_attempt: str | None = None,
        expected_nonce: str | None = None,
        expected_token: str | None = None,
    ) -> dict[str, Any]:
        """Verify that this adapter is bound to the active host attempt and ready to isolate."""
        with self._lock:
            if self._cancelled or self._stopped:
                return {"ok": False, "reason": "attempt_already_stopped_or_cancelled"}
        if self.binding_path is not None:
            try:
                disk_record = _read_private_binding_file(self.binding_path)
            except Exception as exc:
                return {"ok": False, "reason": f"binding_file_invalid:{type(exc).__name__}"}
        else:
            disk_record = {
                "proof_type": "attempt_pre_execution_binding",
                "backend": "bwrap_subreaper",
                "job": self.job,
                "attempt": self.attempt,
                "token": self.token,
                "nonce": self.nonce,
                "instance_id": self.instance_id,
                "generation": self.generation,
                "boot_id": self.boot_id,
                "supervisor_pid": self.supervisor_pid,
                "supervisor_start": self.supervisor_start,
            }

        res = verify_attempt_binding_record(
            disk_record,
            expected_job=expected_job,
            expected_attempt=expected_attempt,
            expected_nonce=expected_nonce,
            expected_token=expected_token,
            expected_supervisor_pid=self.supervisor_pid,
            expected_instance_id=self.instance_id,
            expected_generation=self.generation,
            check_live_supervisor=True,
        )
        if not res.get("ok"):
            return res
        if (disk_record.get("job") != self.job
                or disk_record.get("attempt") != self.attempt
                or disk_record.get("nonce") != self.nonce
                or disk_record.get("token") != self.token):
            return {"ok": False, "reason": "adapter_memory_disk_binding_mismatch"}
        if self.allowed_workdir is not None and not self.allowed_workdir.is_dir():
            return {"ok": False, "reason": "allowed_workdir_missing"}
        res["role"] = self.role
        res["network_policy"] = self.network_policy
        res["allowed_workdir"] = str(self.allowed_workdir) if self.allowed_workdir else None
        res["budgets"] = asdict(self.budgets)
        return res

    def authorize_execution(
        self,
        *,
        target_paths: Sequence[str | Path] = (),
        action: str = "write",
        tool: str | None = None,
        network_access: bool = False,
        payload: Mapping[str, Any] | None = None,
        raise_on_deny: bool = True,
    ) -> dict[str, Any]:
        """Pre-execution check for role path rules, credentials/sockets, tools, and network."""
        from agentcore.colleague_dev import validate_role_path_access

        def _record(allowed: bool, reason: str, offending_path: str | None = None) -> dict[str, Any]:
            entry = {
                "timestamp": time.time(),
                "job": self.job,
                "attempt": self.attempt,
                "instance_id": self.instance_id,
                "role": self.role,
                "action": action,
                "tool": tool,
                "network_access": bool(network_access),
                "target_paths": [str(p) for p in target_paths],
                "offending_path": offending_path,
                "allowed": allowed,
                "reason": reason,
            }
            self.audit_log.append(entry)
            if not allowed and raise_on_deny:
                raise PermissionError(reason)
            return entry

        with self._lock:
            if self._cancelled or self._stopped:
                return _record(False, "attempt_cancelled_or_stopped")

        if payload is not None and isinstance(payload, Mapping):
            forbidden_keys = sorted(FORBIDDEN_PAYLOAD_BINDING_KEYS & set(payload.keys()))
            if forbidden_keys:
                return _record(False, f"payload_binding_self_declaration_forbidden:{','.join(forbidden_keys)}")

        if network_access and self.network_policy != "allowed":
            return _record(False, "unauthorized_network_access_denied")

        if tool is not None and tool not in self.allowed_tools:
            return _record(False, f"unauthorized_tool_denied:{tool}")

        for raw_target in target_paths:
            t_str = str(raw_target)
            t_path = Path(raw_target)
            forbidden, f_reason = is_forbidden_host_path(t_path)
            if forbidden:
                return _record(False, f"forbidden_host_access:{f_reason}", offending_path=t_str)

            if self.allowed_workdir is not None:
                candidate = (self.allowed_workdir / t_path) if not t_path.is_absolute() else t_path
                if candidate.is_symlink():
                    target_resolved = candidate.resolve(strict=False)
                    if not (target_resolved == self.allowed_workdir
                            or target_resolved.is_relative_to(self.allowed_workdir)):
                        return _record(False, "symlink_escape_outside_workdir", offending_path=t_str)
                resolved = candidate.resolve(strict=False)
                if not (resolved == self.allowed_workdir or resolved.is_relative_to(self.allowed_workdir)):
                    return _record(False, "path_outside_authorized_workdir", offending_path=t_str)
                forbidden_res, f_reason_res = is_forbidden_host_path(resolved)
                if forbidden_res:
                    return _record(False, f"forbidden_host_access:{f_reason_res}", offending_path=t_str)
                rel_target = resolved.relative_to(self.allowed_workdir).as_posix()
            else:
                if ".." in Path(t_str).parts:
                    return _record(False, "relative_path_traversal_denied", offending_path=t_str)
                rel_target = t_str

            allowed_role, role_reason = validate_role_path_access(
                self.role,
                rel_target,
                action=action,
                root=self.allowed_workdir,
            )
            if not allowed_role:
                return _record(
                    False,
                    f"role_ownership_violation:role={self.role},action={action},path={rel_target}:{role_reason}",
                    offending_path=t_str,
                )

        return _record(True, "authorized")

    def build_bwrap_argv(
        self,
        argv: Sequence[str],
        workdir: Path,
        *,
        env: Mapping[str, str] | None = None,
        budgets: Optional[ResourceBudgets] = None,
    ) -> list[str]:
        """Construct the bubblewrap command line with filesystem, credential, network, and rlimit isolation."""
        eff_budgets = budgets if budgets is not None else self.budgets
        bwrap_bin = shutil.which("bwrap")
        if not bwrap_bin:
            raise RuntimeError("bwrap_backend_unavailable")
        wdir = workdir.resolve(strict=True)
        if self.allowed_workdir is not None and not (
            wdir == self.allowed_workdir or wdir.is_relative_to(self.allowed_workdir)
        ):
            raise PermissionError("workdir_outside_adapter_allowed_workdir")

        cmd: list[str] = [
            bwrap_bin,
            "--unshare-all",
            "--unshare-user",
            "--disable-userns",
            "--die-with-parent",
            "--new-session",
            "--clearenv",
        ]
        if self.network_policy == "allowed":
            cmd.append("--share-net")

        for sys_root in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/opt"):
            if Path(sys_root).exists():
                cmd.extend(["--ro-bind", sys_root, sys_root])
        for etc_item in ("/etc/ld.so.cache", "/etc/alternatives", "/etc/localtime"):
            if Path(etc_item).exists():
                cmd.extend(["--ro-bind-try", etc_item, etc_item])

        cmd.extend([
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--tmpfs", "/run",
            "--tmpfs", "/var",
            "--tmpfs", "/home",
            "--size", str(eff_budgets.max_disk_bytes),
            "--tmpfs", "/tmp/scratch",
        ])

        bound_py_roots: set[str] = set()
        for raw_py in (Path(sys.base_prefix), Path(sys.prefix), Path(sys.executable).parent.parent):
            for candidate_py in (raw_py, raw_py.resolve(strict=False)):
                c_str = str(candidate_py)
                if not c_str or c_str == "/" or c_str in bound_py_roots or not candidate_py.exists():
                    continue
                if any(
                    candidate_py == Path(sr) or candidate_py.is_relative_to(Path(sr))
                    for sr in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/opt")
                ):
                    continue
                forbidden_py, _ = is_forbidden_host_path(candidate_py)
                if not forbidden_py:
                    cmd.extend(["--ro-bind", c_str, c_str])
                    bound_py_roots.add(c_str)

        for ro_path in self.extra_ro_paths:
            if ro_path.exists():
                cmd.extend(["--ro-bind", str(ro_path), str(ro_path)])

        cmd.extend(["--bind", str(wdir), str(wdir)])

        # Kernel-level read-only enforcement for .git and existing role 'never' paths inside workdir
        from agentcore.colleague_dev import get_role_ownership, validate_role_path_access
        git_entry = wdir / ".git"
        if git_entry.exists():
            cmd.extend(["--ro-bind", str(git_entry), str(git_entry)])

        ownership = get_role_ownership(self.role)
        for candidate in sorted(wdir.rglob("*")):
            if candidate.is_symlink():
                continue
            try:
                rel = candidate.relative_to(wdir).as_posix()
            except ValueError:
                continue
            if rel == ".git" or rel.startswith(".git/"):
                continue
            ok_write, why = validate_role_path_access(self.role, rel, action="write", root=wdir, ownership=ownership)
            if not ok_write:
                if why.startswith("path_in_never_list"):
                    cmd.extend(["--ro-bind", str(candidate), str(candidate)])
                elif candidate.is_file():
                    ok_app, _ = validate_role_path_access(self.role, rel, action="append", root=wdir, ownership=ownership)
                    if not ok_app:
                        cmd.extend(["--ro-bind", str(candidate), str(candidate)])

        py_bin_dir = str(Path(sys.executable).parent)
        sandbox_path = f"{py_bin_dir}:/usr/bin:/bin" if py_bin_dir not in ("/usr/bin", "/bin") else "/usr/bin:/bin"
        cmd.extend([
            "--remount-ro", "/run",
            "--remount-ro", "/var",
            "--remount-ro", "/home",
            "--remount-ro", "/tmp",
            "--remount-ro", "/",
            "--chdir", str(wdir),
            "--setenv", "HOME", "/tmp/scratch",
            "--setenv", "TMPDIR", "/tmp/scratch",
            "--setenv", "PATH", sandbox_path,
            "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
            "--setenv", ENV, self.token,
            "--setenv", "SUPERAGENT_JOB_ID", self.job,
            "--setenv", "SUPERAGENT_ATTEMPT_ID", self.attempt,
            "--setenv", "SUPERAGENT_INSTANCE_ID", self.instance_id,
        ])
        host_ld_lib = os.environ.get("LD_LIBRARY_PATH", "").strip()
        if host_ld_lib:
            cmd.extend(["--setenv", "LD_LIBRARY_PATH", host_ld_lib])

        if self.extra_ro_paths:
            py_paths = ":".join(str(p) for p in self.extra_ro_paths)
            cmd.extend(["--setenv", "PYTHONPATH", py_paths])

        if env:
            for k, v in sorted(env.items()):
                k_str = str(k)
                if any(k_str.startswith(pref) for pref in _SENSITIVE_ENV_PREFIXES):
                    continue
                if k_str in ("HOME", "TMPDIR", ENV, "SUPERAGENT_JOB_ID", "SUPERAGENT_ATTEMPT_ID"):
                    continue
                cmd.extend(["--setenv", k_str, str(v)])

        rlimit_bootstrap = (
            "import os, resource, sys\n"
            f"resource.setrlimit(resource.RLIMIT_AS, ({eff_budgets.max_memory_bytes}, {eff_budgets.max_memory_bytes}))\n"
            f"resource.setrlimit(resource.RLIMIT_FSIZE, ({eff_budgets.max_file_bytes}, {eff_budgets.max_file_bytes}))\n"
            f"resource.setrlimit(resource.RLIMIT_NPROC, ({eff_budgets.max_procs}, {eff_budgets.max_procs}))\n"
            "os.execvp(sys.argv[1], sys.argv[1:])\n"
        )
        bootstrap_py = sys.executable if Path(sys.executable).exists() else "/usr/bin/python3"
        cmd.extend(["--", bootstrap_py, "-c", rlimit_bootstrap, *[str(a) for a in argv]])
        return cmd

    @staticmethod
    def is_bwrap_functional() -> bool:
        """Check whether bubblewrap is installed and permitted to create user/mount namespaces on this host."""
        bwrap_bin = shutil.which("bwrap")
        if not bwrap_bin:
            return False
        true_bin = shutil.which("true") or "/bin/true"
        try:
            probe = subprocess.run(
                [bwrap_bin, "--unshare-user", "--die-with-parent", "--ro-bind", "/", "/", "--", true_bin],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3.0,
            )
            return probe.returncode == 0
        except Exception:
            return False

    def build_worker_bwrap_argv(
        self,
        argv: Sequence[str],
        workdir: Path,
        *,
        attempt_dir: Optional[Path] = None,
        writable_paths: Sequence[str | Path] = (),
        hermes_runtime_home: Optional[str | Path] = None,
        hermes_pm_writable_paths: Sequence[str | Path] = (),
        managed_state_paths: Sequence[str | Path] = (),
    ) -> list[str]:
        """Construct a production worker bubblewrap command line with read-only root, isolated /tmp, and masked credentials."""
        bwrap_bin = shutil.which("bwrap")
        if not bwrap_bin:
            raise RuntimeError("bwrap_backend_unavailable")
        wdir = workdir.resolve(strict=True)
        if self.allowed_workdir is not None and not (
            wdir == self.allowed_workdir or wdir.is_relative_to(self.allowed_workdir)
        ):
            raise PermissionError("workdir_outside_adapter_allowed_workdir")

        cmd: list[str] = [
            bwrap_bin,
            "--unshare-user",
            "--die-with-parent",
            "--new-session",
        ]
        if self.network_policy == "allowed":
            cmd.append("--share-net")
        else:
            cmd.append("--unshare-net")

        cmd.extend([
            "--ro-bind", "/", "/",
            "--dev", "/dev",
            "--proc", "/proc",
            "--tmpfs", "/tmp",
        ])

        bound_rw: set[str] = set()
        ro_armored_paths: list[str] = []
        home_dir = Path.home().resolve(strict=False)
        hermes_root = home_dir / ".hermes"
        super_agent_root = home_dir / "super_agent"
        agentcore_state_root = home_dir / ".local" / "state" / "agentcore"

        def _bind_rw_unchecked(resolved: Path) -> bool:
            r_str = str(resolved)
            if r_str in bound_rw or r_str == "/":
                return False
            cmd.extend(["--bind", r_str, r_str])
            bound_rw.add(r_str)
            return True

        def _bind_rw(target: Path | str | None) -> None:
            if not target:
                return
            try:
                p = Path(target).expanduser()
                if not p.exists():
                    return
                resolved = p.resolve(strict=True)
            except OSError:
                return
            forb, _ = is_forbidden_host_path(resolved)
            if forb:
                return
            _bind_rw_unchecked(resolved)

        _bind_rw(wdir)
        if attempt_dir is not None:
            try:
                att_p = Path(attempt_dir).expanduser()
                if att_p.is_dir() and not att_p.is_symlink():
                    att_res = att_p.resolve(strict=True)
                    att_allowed = (
                        not is_forbidden_host_path(att_res)[0]
                        or (att_res.is_relative_to(super_agent_root) and att_res.parent.name == "attempts")
                    )
                    if att_allowed:
                        _bind_rw_unchecked(att_res)
                        binding_file = att_res / "binding.json"
                        if binding_file.is_file():
                            b_res = str(binding_file.resolve(strict=True))
                            cmd.extend(["--ro-bind", b_res, b_res])
                            ro_armored_paths.append(b_res)
            except OSError:
                pass

        for wp in writable_paths:
            _bind_rw(wp)

        if hermes_runtime_home is not None:
            try:
                hr_p = Path(hermes_runtime_home).expanduser()
                if hr_p.is_dir():
                    hr_res = hr_p.resolve(strict=True)
                    installs_root = hermes_root / "installs"
                    in_hermes_profile = (
                        (hr_res == hermes_root or hr_res.is_relative_to(hermes_root))
                        and hr_res != installs_root
                        and not hr_res.is_relative_to(installs_root)
                    )
                    if in_hermes_profile or not is_forbidden_host_path(hr_res)[0]:
                        _bind_rw_unchecked(hr_res)
                        for rel_guard in (
                            "config.yaml",
                            "profile.yaml",
                            "SOUL.md",
                            "AGENTS.md",
                            "CLAUDE.md",
                            ".env",
                            "auth.json",
                            "llm_config.json",
                            "plugins",
                            "skills",
                            "hooks",
                            "pairing",
                            "worker",
                            "cron/jobs.json",
                        ):
                            guard_candidate = hr_res / rel_guard
                            try:
                                if guard_candidate.exists():
                                    g_str = str(guard_candidate.resolve(strict=True))
                                    cmd.extend(["--ro-bind", g_str, g_str])
                                    ro_armored_paths.append(g_str)
                            except OSError:
                                continue
            except OSError:
                pass

        for pm_target in hermes_pm_writable_paths:
            if not pm_target:
                continue
            try:
                pm_res = Path(pm_target).expanduser().resolve(strict=True)
            except OSError:
                continue
            is_shared_runtime_dir = pm_res == (hermes_root / "runtime") and pm_res.is_dir()
            if not is_shared_runtime_dir and pm_res.name not in {".install.lock", ".prepare.lock", ".leases"}:
                continue
            if (
                is_shared_runtime_dir
                or pm_res.is_relative_to(hermes_root / "installs")
                or not is_forbidden_host_path(pm_res)[0]
            ):
                _bind_rw_unchecked(pm_res)

        for ms_target in managed_state_paths:
            if not ms_target:
                continue
            try:
                ms_res = Path(ms_target).expanduser().resolve(strict=True)
            except OSError:
                continue
            if not ms_res.is_dir() or ms_res.name not in {
                "memory-spool",
                "recall-diagnostics",
                "personal-memory-candidate",
            }:
                continue
            if (
                ms_res.is_relative_to(super_agent_root)
                or ms_res.is_relative_to(agentcore_state_root)
                or not is_forbidden_host_path(ms_res)[0]
            ):
                _bind_rw_unchecked(ms_res)

        if argv:
            seen_tmp_ro: set[str] = set()
            for arg_item in argv:
                try:
                    exe_p = Path(str(arg_item)).expanduser()
                    if exe_p.is_file():
                        exe_res = exe_p.resolve(strict=True)
                        exe_str = str(exe_res)
                        if (
                            exe_str not in seen_tmp_ro
                            and exe_str not in bound_rw
                            and not any(exe_str == bp or exe_str.startswith(bp + "/") for bp in bound_rw)
                            and exe_res.is_relative_to(Path("/tmp"))
                        ):
                            cmd.extend(["--ro-bind", exe_str, exe_str])
                            seen_tmp_ro.add(exe_str)
                except OSError:
                    continue

        # Mask host credential directories and files after writable binds so they remain inaccessible and read-only
        masked_credentials: list[str] = []
        for cred_dir_name in (".ssh", ".gnupg", ".aws", ".kube", ".docker"):
            cred_dir = home_dir / cred_dir_name
            if cred_dir.is_dir() and not cred_dir.is_symlink():
                c_str = str(cred_dir)
                cmd.extend(["--tmpfs", c_str, "--remount-ro", c_str])
                masked_credentials.append(c_str)
        for cred_file_name in (".netrc", ".git-credentials", ".npmrc", ".pypirc"):
            cred_file = home_dir / cred_file_name
            if cred_file.is_file() and not cred_file.is_symlink():
                c_str = str(cred_file)
                cmd.extend(["--ro-bind", "/dev/null", c_str])
                masked_credentials.append(c_str)

        cmd.extend([
            "--setenv", ENV, self.token,
            "--setenv", "SUPERAGENT_JOB_ID", self.job,
            "--setenv", "SUPERAGENT_ATTEMPT_ID", self.attempt,
            "--setenv", "SUPERAGENT_INSTANCE_ID", self.instance_id,
            "--setenv", "SUPERAGENT_SANDBOX_BACKEND", "bwrap",
            "--chdir", str(wdir),
            "--",
            *[str(a) for a in argv],
        ])

        self.audit_log.append({
            "timestamp": time.time(),
            "job": self.job,
            "attempt": self.attempt,
            "instance_id": self.instance_id,
            "role": self.role,
            "action": "build_worker_bwrap_argv",
            "tool": "bwrap",
            "workdir": str(wdir),
            "attempt_dir": str(attempt_dir) if attempt_dir else None,
            "writable_binds": sorted(bound_rw),
            "ro_armored_paths": sorted(set(ro_armored_paths)),
            "masked_credentials": masked_credentials,
            "allowed": True,
            "reason": "worker_bwrap_sandbox_prepared",
        })
        return cmd

    def active_sandbox_pids(self) -> list[int]:
        """Return live host PIDs currently executing inside this adapter's sandbox tree."""
        with self._lock:
            spid = self._active_subreaper_pid
        if not spid:
            return []
        desc = _descendant_pids(spid)
        live = [p for p in [spid, *desc] if process_start(p) is not None]
        with self._lock:
            self._observed_sandbox_pids.update(live)
        return live

    def execute_in_sandbox(
        self,
        argv: Sequence[str],
        timeout_s: Optional[float] = None,
        workdir: Optional[Path | str] = None,
        *,
        env: Mapping[str, str] | None = None,
        target_paths: Sequence[str | Path] = (),
        action: str = "write",
        tool: str = "bash",
        network_access: bool = False,
        payload: Mapping[str, Any] | None = None,
        budgets: Optional[ResourceBudgets | Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """Execute argv inside the attempt-bound subreaper + bubblewrap sandbox."""
        if not argv or any(not isinstance(a, str) for a in argv):
            raise ValueError("sandbox_argv_required")
        eff_budgets = normalize_resource_budgets(budgets) if budgets is not None else self.budgets
        binding_check = self.verify_attempt_binding(
            expected_job=self.job,
            expected_attempt=self.attempt,
            expected_nonce=self.nonce,
            expected_token=self.token,
        )
        if not binding_check.get("ok"):
            raise PermissionError(f"attempt_binding_verification_failed:{binding_check.get('reason')}")

        self.authorize_execution(
            target_paths=target_paths,
            action=action,
            tool=tool,
            network_access=network_access,
            payload=payload,
            raise_on_deny=True,
        )

        target_workdir = Path(workdir).resolve(strict=True) if workdir is not None else self.allowed_workdir
        if target_workdir is None or not target_workdir.is_dir():
            raise ValueError("sandbox_workdir_required")

        eff_timeout = float(eff_budgets.timeout_s if timeout_s is None else min(float(timeout_s), eff_budgets.timeout_s))
        if not math.isfinite(eff_timeout) or eff_timeout <= 0:
            raise ValueError("invalid_resource_budget:timeout_s")

        bwrap_argv = self.build_bwrap_argv(argv, target_workdir, env=env, budgets=eff_budgets)
        pre_snap = _snapshot_workdir(target_workdir)
        pre_total_bytes = sum(item["size"] for item in pre_snap.values())

        ctrl_parent, ctrl_child = socket.socketpair(type=socket.SOCK_STREAM)
        res_r, res_w = os.pipe()
        proc = None
        t0 = time.monotonic()
        try:
            with self._lock:
                if self._cancelled or self._stopped:
                    raise PermissionError("attempt_cancelled_or_stopped")
                self._active_executions += 1

            subreaper_cmd = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--sandbox-subreaper",
                str(ctrl_child.fileno()),
                str(res_w),
            ]
            subreaper_env = os.environ.copy()
            subreaper_env[ENV] = self.token
            proc = subprocess.Popen(
                subreaper_cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                pass_fds=(ctrl_child.fileno(), res_w),
                close_fds=True,
                env=subreaper_env,
            )
            ctrl_child.close()
            ctrl_child = None
            os.close(res_w)
            res_w = None

            cfg_blob = json.dumps({
                "bwrap_argv": bwrap_argv,
                "timeout_s": eff_timeout,
                "max_output_bytes": eff_budgets.max_output_bytes,
            }).encode("utf-8") + b"\n"
            ctrl_parent.sendall(cfg_blob)

            with self._lock:
                self._active_control = ctrl_parent
                self._active_subreaper_pid = proc.pid
                self._observed_sandbox_pids.add(proc.pid)
                if self._cancelled:
                    try:
                        ctrl_parent.sendall(b"stop\n")
                    except OSError:
                        pass

            ctrl_parent.settimeout(5.0)
            try:
                started_msg = ctrl_parent.recv(4096)
                if started_msg:
                    info = json.loads(started_msg.decode("utf-8").splitlines()[0])
                    bpid = info.get("bwrap_pid")
                    if isinstance(bpid, int) and bpid > 0:
                        with self._lock:
                            self._active_bwrap_pid = bpid
                            self._observed_sandbox_pids.add(bpid)
            except Exception:
                pass
            finally:
                ctrl_parent.settimeout(None)

            while proc.poll() is None:
                self.active_sandbox_pids()
                with self._lock:
                    if self._cancelled:
                        try:
                            ctrl_parent.sendall(b"stop\n")
                        except OSError:
                            pass
                time.sleep(0.02)

            proc.wait(timeout=5.0)
            raw_res = bytearray()
            while True:
                chunk = os.read(res_r, 65536)
                if not chunk:
                    break
                raw_res.extend(chunk)
            sub_out = json.loads(raw_res.decode("utf-8")) if raw_res else {
                "exit_code": -1,
                "stdout": "",
                "stderr": "subreaper_empty_response",
                "timed_out": False,
                "output_limited": False,
                "cancelled": self._cancelled,
                "quiescent": False,
                "unquiesced_pids": [],
            }
        finally:
            if proc is not None and proc.poll() is None:
                try:
                    proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    pass
            with self._lock:
                self._active_control = None
                self._active_subreaper_pid = None
                self._active_bwrap_pid = None
                if self._active_executions > 0:
                    self._active_executions -= 1
            if ctrl_child is not None:
                ctrl_child.close()
            if res_w is not None:
                os.close(res_w)
            os.close(res_r)
            ctrl_parent.close()

        for opid in sub_out.get("observed_pids", []):
            if isinstance(opid, int) and opid > 0:
                with self._lock:
                    self._observed_sandbox_pids.add(opid)

        # Post-execution worktree disk budget & role path ownership verification
        post_snap = _snapshot_workdir(target_workdir)
        post_total_bytes = sum(item["size"] for item in post_snap.values())
        disk_delta = max(0, post_total_bytes - pre_total_bytes)

        budget_exceeded: Optional[str] = None
        if sub_out.get("timed_out"):
            budget_exceeded = "timeout"
        elif sub_out.get("output_limited"):
            budget_exceeded = "output"
        elif disk_delta > eff_budgets.max_disk_bytes:
            budget_exceeded = "disk"
            for rel, meta in post_snap.items():
                if rel not in pre_snap:
                    _unlink_entry(target_workdir / rel)
                elif meta["sha256"] != pre_snap[rel]["sha256"]:
                    _restore_snapshot_entry(target_workdir, rel, pre_snap[rel])
            for rel in set(pre_snap) - set(post_snap):
                _restore_snapshot_entry(target_workdir, rel, pre_snap[rel])

        policy_violation = False
        policy_violation_reason = ""
        from agentcore.colleague_dev import validate_role_path_access

        def _check_symlink_target_safe(rel_path: str, link_target_str: str) -> tuple[bool, str]:
            link_file = target_workdir / rel_path
            raw_t = Path(link_target_str).expanduser()
            forb_raw, f_why_raw = is_forbidden_host_path(raw_t)
            if forb_raw:
                return False, f"post_exec_symlink_forbidden_host_path:{rel_path}:{f_why_raw}"
            cand = (link_file.parent / raw_t) if not raw_t.is_absolute() else raw_t
            resolved_t = cand.resolve(strict=False)
            forb_res, f_why_res = is_forbidden_host_path(resolved_t)
            if forb_res:
                return False, f"post_exec_symlink_forbidden_host_path:{rel_path}:{f_why_res}"
            if not (resolved_t == target_workdir or resolved_t.is_relative_to(target_workdir)):
                return False, f"post_exec_symlink_escape_outside_workdir:{rel_path}->{link_target_str}"
            if resolved_t != target_workdir:
                t_rel = resolved_t.relative_to(target_workdir).as_posix()
                if t_rel == ".git" or t_rel.startswith(".git/"):
                    return False, f"post_exec_symlink_to_git_denied:{rel_path}"
                ok_tw, why_tw = validate_role_path_access(self.role, t_rel, action="write", root=target_workdir)
                if not ok_tw:
                    return False, f"post_exec_symlink_target_role_denied:{rel_path}->{t_rel}:{why_tw}"
            return True, "ok"

        for rel, meta in post_snap.items():
            if rel not in pre_snap:
                ok_w, reason_w = validate_role_path_access(self.role, rel, action="write", root=target_workdir)
                if not ok_w:
                    policy_violation = True
                    policy_violation_reason = f"post_exec_create_denied:{rel}:{reason_w}"
                    _unlink_entry(target_workdir / rel)
                    continue
                if meta.get("is_symlink"):
                    ok_sym, sym_reason = _check_symlink_target_safe(rel, str(meta.get("link_target") or ""))
                    if not ok_sym:
                        policy_violation = True
                        policy_violation_reason = sym_reason
                        _unlink_entry(target_workdir / rel)
            elif meta["sha256"] != pre_snap[rel]["sha256"]:
                if meta.get("is_symlink"):
                    ok_w, reason_w = validate_role_path_access(self.role, rel, action="write", root=target_workdir)
                    if not ok_w:
                        policy_violation = True
                        policy_violation_reason = f"post_exec_modify_denied:{rel}:{reason_w}"
                        _restore_snapshot_entry(target_workdir, rel, pre_snap[rel])
                        continue
                    ok_sym, sym_reason = _check_symlink_target_safe(rel, str(meta.get("link_target") or ""))
                    if not ok_sym:
                        policy_violation = True
                        policy_violation_reason = sym_reason
                        _restore_snapshot_entry(target_workdir, rel, pre_snap[rel])
                        continue
                else:
                    ok_w, reason_w = validate_role_path_access(self.role, rel, action="write", root=target_workdir)
                    if not ok_w:
                        ok_app, _ = validate_role_path_access(self.role, rel, action="append", root=target_workdir)
                        old_size = int(pre_snap[rel]["size"])
                        if (ok_app
                                and not pre_snap[rel].get("is_symlink")
                                and int(meta["size"]) >= old_size
                                and _file_prefix_sha256(target_workdir / rel, old_size) == pre_snap[rel]["sha256"]):
                            continue
                        policy_violation = True
                        policy_violation_reason = f"post_exec_modify_denied:{rel}:{reason_w}"
                        _restore_snapshot_entry(target_workdir, rel, pre_snap[rel])

        for rel in set(pre_snap) - set(post_snap):
            ok_del, reason_del = validate_role_path_access(self.role, rel, action="write", root=target_workdir)
            if not ok_del:
                policy_violation = True
                policy_violation_reason = f"post_exec_delete_denied:{rel}:{reason_del}"
                _restore_snapshot_entry(target_workdir, rel, pre_snap[rel])

        if policy_violation:
            self.audit_log.append({
                "timestamp": time.time(),
                "job": self.job,
                "attempt": self.attempt,
                "instance_id": self.instance_id,
                "role": self.role,
                "action": "post_exec_audit",
                "allowed": False,
                "reason": policy_violation_reason,
            })

        exit_code = int(sub_out.get("exit_code", -1))
        stderr_text = str(sub_out.get("stderr", ""))
        if budget_exceeded == "disk":
            exit_code = 125
            stderr_text += f"\nDisk budget exceeded: delta {disk_delta} > {eff_budgets.max_disk_bytes} bytes"
        if policy_violation:
            exit_code = 126
            stderr_text += f"\nRole path policy violation: {policy_violation_reason}"

        quiescent = bool(sub_out.get("quiescent")) and not sub_out.get("unquiesced_pids")
        proof = {
            "job": self.job,
            "attempt": self.attempt,
            "token": self.token,
            "nonce": self.nonce,
            "instance_id": self.instance_id,
            "generation": self.generation,
            "supervisor_pid": self.supervisor_pid,
            "supervisor_start": self.supervisor_start,
            "sandbox_subreaper_pid": sub_out.get("subreaper_pid"),
            "quiescent": quiescent,
            "proof_scope": "subreaper_tree",
            "exit_code": exit_code,
            "child_started": True,
            "unquiesced_pids": sub_out.get("unquiesced_pids", []),
        }
        with self._lock:
            self._last_stop_proof = proof

        execution_error = bool(
            sub_out.get("timed_out")
            or sub_out.get("output_limited")
            or budget_exceeded is not None
            or policy_violation
        )
        cancelled_flag = bool(sub_out.get("cancelled") or self._cancelled)
        return {
            "ok": (exit_code == 0) and not execution_error and not cancelled_flag and quiescent,
            "exit_code": exit_code,
            "stdout": str(sub_out.get("stdout", "")),
            "stderr": stderr_text,
            "timed_out": bool(sub_out.get("timed_out")),
            "output_limited": bool(sub_out.get("output_limited")),
            "cancelled": cancelled_flag,
            "budget_exceeded": budget_exceeded,
            "policy_violation": policy_violation,
            "policy_violation_reason": policy_violation_reason,
            "execution_error": execution_error,
            "quiescent": quiescent,
            "unquiesced_pids": sub_out.get("unquiesced_pids", []),
            "observed_pids": sorted(self._observed_sandbox_pids),
            "duration_s": time.monotonic() - t0,
            "stop_proof": proof,
        }

    def verification_executor(
        self,
        argv: Sequence[str],
        timeout_s: float,
        workdir: Path,
        *,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Verification gate executor running checks inside the attempt-bound sandbox."""
        return self.execute_in_sandbox(
            argv,
            timeout_s=timeout_s,
            workdir=workdir,
            env=env,
            action="read",
            tool="pytest",
        )

    def cancel(
        self,
        *,
        expected_job: Optional[str] = None,
        expected_attempt: Optional[str] = None,
        expected_nonce: Optional[str] = None,
        expected_token: Optional[str] = None,
    ) -> dict[str, Any]:
        """Two-phase cancel: block new actions first, then terminate sandbox and verify descendants.

        Missing binding files, dead/mismatched host supervisors, or surviving descendants
        preserve status='unknown' and quiescent=False without fabricating stop proof.
        """
        with self._lock:
            self._cancelled = True
            ctrl = self._active_control
            active_spid = self._active_subreaper_pid

        # Step 1: Send stop signal to any active sandbox execution immediately and wait until all executions quiesce
        if ctrl is not None:
            try:
                ctrl.sendall(b"stop\n")
            except OSError:
                pass

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with self._lock:
                cur_ctrl = self._active_control
                if self._active_executions == 0 and self._active_subreaper_pid is None:
                    break
            if cur_ctrl is not None:
                try:
                    cur_ctrl.sendall(b"stop\n")
                except OSError:
                    pass
            time.sleep(0.02)

        # Step 2: Validate binding identity and supervisor state (do not forge stop proof on mismatch/missing)
        exp_job = expected_job if expected_job is not None else self.job
        exp_att = expected_attempt if expected_attempt is not None else self.attempt
        exp_non = expected_nonce if expected_nonce is not None else self.nonce
        exp_tok = expected_token if expected_token is not None else self.token

        if self.binding_path is not None:
            try:
                disk_record = _read_private_binding_file(self.binding_path)
            except Exception as exc:
                return {
                    "ok": False,
                    "status": "unknown",
                    "quiescent": False,
                    "new_actions_blocked": True,
                    "reason": f"binding_missing_or_unreadable:{type(exc).__name__}",
                }
        else:
            disk_record = {
                "proof_type": "attempt_pre_execution_binding",
                "backend": "bwrap_subreaper",
                "job": self.job,
                "attempt": self.attempt,
                "token": self.token,
                "nonce": self.nonce,
                "instance_id": self.instance_id,
                "generation": self.generation,
                "boot_id": self.boot_id,
                "supervisor_pid": self.supervisor_pid,
                "supervisor_start": self.supervisor_start,
            }

        bcheck = verify_attempt_binding_record(
            disk_record,
            expected_job=exp_job,
            expected_attempt=exp_att,
            expected_nonce=exp_non,
            expected_token=exp_tok,
            expected_supervisor_pid=self.supervisor_pid,
            expected_instance_id=self.instance_id,
            expected_generation=self.generation,
            check_live_supervisor=True,
        )
        if not bcheck.get("ok"):
            return {
                "ok": False,
                "status": "unknown",
                "quiescent": False,
                "new_actions_blocked": True,
                "reason": bcheck.get("reason", "binding_verification_failed"),
            }

        # Step 3: Verify all observed sandbox descendants are gone
        survivors: list[int] = []
        if active_spid and process_start(active_spid) is not None:
            survivors.append(active_spid)
        for pid in sorted(self._observed_sandbox_pids):
            if process_start(pid) is not None:
                if pid not in survivors:
                    survivors.append(pid)
        tagged_survivors = tagged(self.token)
        for pid in tagged_survivors:
            if pid not in survivors:
                survivors.append(pid)

        if survivors:
            return {
                "ok": False,
                "status": "unknown",
                "quiescent": False,
                "new_actions_blocked": True,
                "reason": "unquiesced_descendants_remain",
                "unquiesced_pids": sorted(survivors),
            }

        with self._lock:
            self._stopped = True
            proof = self._last_stop_proof or {
                "job": self.job,
                "attempt": self.attempt,
                "token": self.token,
                "nonce": self.nonce,
                "instance_id": self.instance_id,
                "generation": self.generation,
                "supervisor_pid": self.supervisor_pid,
                "supervisor_start": self.supervisor_start,
                "quiescent": True,
                "proof_scope": "subreaper_tree",
                "exit_code": -signal.SIGTERM,
                "child_started": False,
                "unquiesced_pids": [],
            }
            self._last_stop_proof = proof

        verified = verify_subreaper_containment(
            proof,
            expected_token=self.token,
            expected_job=self.job,
            expected_attempt=self.attempt,
            expected_nonce=self.nonce,
            expected_supervisor_pid=self.supervisor_pid,
        )
        if not verified.get("ok"):
            return {
                "ok": False,
                "status": "unknown",
                "quiescent": False,
                "new_actions_blocked": True,
                "reason": verified.get("reason", "stop_verification_failed"),
            }
        verified["new_actions_blocked"] = True
        verified["status"] = "stopped"
        return verified

    def trusted_containment_evidence(self) -> Optional[dict[str, Any]]:
        """Return the subreaper quiescence proof for the completed sandbox execution."""
        with self._lock:
            if self._active_executions > 0 or self._active_subreaper_pid is not None:
                return None
            if self._last_stop_proof is None:
                return None
            observed = sorted(self._observed_sandbox_pids)
            proof = dict(self._last_stop_proof)
        survivors = [pid for pid in observed if process_start(pid) is not None]
        if survivors:
            proof["quiescent"] = False
            proof["unquiesced_pids"] = survivors
        return proof


def _sandbox_subreaper_main(ctrl_fd: int, res_fd: int) -> int:
    """Dedicated single-threaded subreaper process supervising one bwrap invocation."""
    repo_root = str(Path(__file__).resolve().parents[1])
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from agentcore.attempt_supervisor import _enable_subreaper, _kill_children

    ctrl = socket.socket(fileno=ctrl_fd)
    observed_pids: set[int] = {os.getpid()}
    bwrap_proc: Optional[subprocess.Popen] = None
    timed_out = False
    output_limited = False
    cancelled = False
    exit_code = -1
    buffers = {"stdout": bytearray(), "stderr": bytearray()}

    try:
        _enable_subreaper()
        raw_cfg = bytearray()
        while b"\n" not in raw_cfg and len(raw_cfg) < 65536:
            chunk = ctrl.recv(4096)
            if not chunk:
                cancelled = True
                break
            raw_cfg.extend(chunk)
        lines = [ln.strip() for ln in raw_cfg.decode("utf-8", errors="replace").splitlines() if ln.strip()]
        cfg_line = None
        for ln in lines:
            if ln.startswith("{"):
                cfg_line = ln
            elif ln == "stop":
                cancelled = True
        if cfg_line is None:
            cancelled = True
            cfg = {"bwrap_argv": [], "timeout_s": 1.0, "max_output_bytes": 4096}
        else:
            cfg = json.loads(cfg_line)
        bwrap_argv = cfg["bwrap_argv"]
        timeout_s = float(cfg["timeout_s"])
        max_output_bytes = int(cfg["max_output_bytes"])

        if not cancelled and bwrap_argv:
            bwrap_env = {"PATH": "/usr/bin:/bin"}
            if os.environ.get(ENV):
                bwrap_env[ENV] = os.environ[ENV]
            bwrap_proc = subprocess.Popen(
                bwrap_argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                close_fds=True,
                env=bwrap_env,
            )
            observed_pids.add(bwrap_proc.pid)
            try:
                ctrl.sendall((json.dumps({
                    "event": "started",
                    "bwrap_pid": bwrap_proc.pid,
                    "subreaper_pid": os.getpid(),
                }) + "\n").encode("utf-8"))
            except OSError:
                pass

            deadline = time.monotonic() + timeout_s
            with selectors.DefaultSelector() as sel:
                for name, stream in (("stdout", bwrap_proc.stdout), ("stderr", bwrap_proc.stderr)):
                    os.set_blocking(stream.fileno(), False)
                    sel.register(stream, selectors.EVENT_READ, name)
                ctrl.setblocking(False)
                sel.register(ctrl, selectors.EVENT_READ, "ctrl")

                while True:
                    observed_pids.update(_descendant_pids(os.getpid()))
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    stream_keys = [k for k in sel.get_map().values() if k.data in ("stdout", "stderr")]
                    if not stream_keys and bwrap_proc.poll() is not None:
                        break
                    events = sel.select(min(0.02, remaining))
                    stop_now = False
                    for key, _ in events:
                        if key.data == "ctrl":
                            try:
                                msg = ctrl.recv(1024)
                            except BlockingIOError:
                                continue
                            except OSError:
                                msg = b""
                            # Any message ('stop') or EOF (parent worker crash) triggers immediate stop!
                            cancelled = True
                            stop_now = True
                            break
                        else:
                            try:
                                chunk = os.read(key.fd, 65536)
                            except BlockingIOError:
                                continue
                            if not chunk:
                                sel.unregister(key.fileobj)
                                key.fileobj.close()
                                continue
                            target = buffers[key.data]
                            room = max_output_bytes - len(target)
                            target.extend(chunk[:room])
                            if len(chunk) > room:
                                output_limited = True
                                stop_now = True
                                break
                    if stop_now:
                        break

            if not timed_out and not output_limited and not cancelled:
                try:
                    bwrap_proc.wait(timeout=max(0.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    timed_out = True
        else:
            try:
                ctrl.sendall((json.dumps({
                    "event": "cancelled_before_spawn",
                    "bwrap_pid": None,
                    "subreaper_pid": os.getpid(),
                }) + "\n").encode("utf-8"))
            except OSError:
                pass
            exit_code = -signal.SIGTERM

        # Reap all children and adopted grandchildren deterministically until ECHILD
        reap_deadline = time.monotonic() + 5.0
        unquiesced_pids: list[int] = []
        quiescent = True
        while True:
            observed_pids.update(_descendant_pids(os.getpid()))
            if bwrap_proc is not None and bwrap_proc.returncode is None:
                try:
                    os.killpg(bwrap_proc.pid, signal.SIGKILL)
                except OSError:
                    pass
            unquiesced_pids = _kill_children()
            no_children = False
            while True:
                try:
                    wpid, wstatus = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    no_children = True
                    break
                except InterruptedError:
                    continue
                if wpid == 0:
                    break
                observed_pids.add(wpid)
                if bwrap_proc is not None and wpid == bwrap_proc.pid:
                    exit_code = os.waitstatus_to_exitcode(wstatus)
                    bwrap_proc.returncode = exit_code
            if no_children:
                quiescent = True
                unquiesced_pids = []
                break
            if time.monotonic() >= reap_deadline:
                quiescent = False
                rem = _descendant_pids(os.getpid())
                unquiesced_pids = sorted(set(unquiesced_pids + rem))
                break
            time.sleep(0.01)

        if bwrap_proc is not None and exit_code == -1 and bwrap_proc.returncode is not None:
            exit_code = bwrap_proc.returncode
        if timed_out:
            exit_code = 124
        elif cancelled and exit_code == 0:
            exit_code = -signal.SIGTERM

        out_str = buffers["stdout"].decode("utf-8", errors="replace")
        err_str = buffers["stderr"].decode("utf-8", errors="replace")
        if output_limited:
            err_str += f"\n[TRUNCATED: output exceeded {max_output_bytes} bytes]"
        if timed_out:
            err_str += f"\nCommand timed out after {timeout_s}s"

        res_payload = json.dumps({
            "exit_code": exit_code,
            "stdout": out_str,
            "stderr": err_str,
            "timed_out": timed_out,
            "output_limited": output_limited,
            "cancelled": cancelled,
            "quiescent": quiescent,
            "unquiesced_pids": unquiesced_pids,
            "subreaper_pid": os.getpid(),
            "bwrap_pid": bwrap_proc.pid if bwrap_proc is not None else None,
            "observed_pids": sorted(observed_pids),
        }).encode("utf-8")
        written = 0
        while written < len(res_payload):
            written += os.write(res_fd, res_payload[written:])
    finally:
        if bwrap_proc is not None:
            for s in (bwrap_proc.stdout, bwrap_proc.stderr):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass
        try:
            ctrl.close()
        except OSError:
            pass
        try:
            os.close(res_fd)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--sandbox-subreaper":
        raise SystemExit(_sandbox_subreaper_main(int(sys.argv[2]), int(sys.argv[3])))
