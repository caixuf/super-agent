"""Deterministic gate contracts and safe verification execution harnesses.

Ported and adapted from open-source gate contracts and verification principles:
- Enforces strict contract schemas for executable verification checks.
- Runs checks under bounded timeouts with output truncation and digest extraction.
- Fails closed on critical failures to halt cascading corrupted executions.
- Supports reproduction checks (expecting non-zero exits) as well as regular tests.
- Issues tamper-evident, privacy-preserving verification receipts.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import time
from typing import Any, Callable, Final, Mapping, Sequence

VERIFICATION_CHECK_CONTRACT_SCHEMA: Final[str] = "verification_check_contract/v1"
VERIFICATION_RECEIPT_SCHEMA: Final[str] = "verification_receipt/v1"

MAX_VERIFICATION_CHECKS: Final[int] = 8
MAX_VERIFICATION_COMMAND_CHARS: Final[int] = 240
MAX_VERIFICATION_TIMEOUT: Final[float] = 300.0
DEFAULT_CHECK_TIMEOUT: Final[float] = 30.0
MAX_OUTPUT_BYTES: Final[int] = 1_048_576

VERIFICATION_SCOPES: Final[tuple[str, ...]] = (
    "unit_scoped",
    "workspace_scoped",
    "environment_scoped",
)
SAFETY_TIERS: Final[tuple[str, ...]] = ("read_only", "isolated_test", "mutating")
VERIFICATION_STATUSES: Final[tuple[str, ...]] = (
    "passed",
    "failed",
    "timed_out",
    "error",
    "skipped",
)

_CHECK_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")
_ENV_ASSIGNMENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*=.*$")


@dataclass(frozen=True)
class VerificationCheck:
    check_id: str
    command: tuple[str, ...]
    timeout_s: float = DEFAULT_CHECK_TIMEOUT
    scope: str = "unit_scoped"
    safety_tier: str = "read_only"
    expected_exit_code: int = 0
    critical: bool = True
    env: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not _CHECK_ID_RE.fullmatch(self.check_id):
            raise ValueError(f"invalid check_id: {self.check_id!r}")
        if not isinstance(self.command, tuple):
            object.__setattr__(self, "command", tuple(self.command))
        if not isinstance(self.env, tuple):
            object.__setattr__(self, "env", tuple(self.env))
        if not self.command:
            raise ValueError("verification command cannot be empty")
        raw_cmd = " ".join(self.command)
        if len(raw_cmd) > MAX_VERIFICATION_COMMAND_CHARS:
            raise ValueError(
                f"command length ({len(raw_cmd)}) exceeds cap {MAX_VERIFICATION_COMMAND_CHARS}"
            )
        if self.timeout_s <= 0 or self.timeout_s > MAX_VERIFICATION_TIMEOUT:
            raise ValueError(f"timeout_s must be in (0, {MAX_VERIFICATION_TIMEOUT}]")
        if self.scope not in VERIFICATION_SCOPES:
            raise ValueError(f"scope must be in {VERIFICATION_SCOPES}")
        if self.safety_tier not in SAFETY_TIERS:
            raise ValueError(f"safety_tier must be in {SAFETY_TIERS}")


def normalize_verification_check(raw: Mapping[str, Any] | VerificationCheck) -> VerificationCheck:
    """Normalize and validate a verification check definition."""
    if isinstance(raw, VerificationCheck):
        return raw

    if not isinstance(raw, Mapping):
        raise TypeError("verification check must be a Mapping or VerificationCheck")

    check_id = str(raw.get("check_id", "")).strip()
    cmd_raw = raw.get("command")
    env_dict: dict[str, str] = {}

    if isinstance(cmd_raw, str):
        tokens = list(shlex.split(cmd_raw.strip()))
        while tokens and _ENV_ASSIGNMENT_RE.match(tokens[0]):
            k, _, v = tokens.pop(0).partition("=")
            env_dict[k] = v
        cmd_tokens = tuple(tokens)
    elif isinstance(cmd_raw, (list, tuple)):
        tokens = [str(x) for x in cmd_raw]
        while tokens and _ENV_ASSIGNMENT_RE.match(tokens[0]):
            k, _, v = tokens.pop(0).partition("=")
            env_dict[k] = v
        cmd_tokens = tuple(tokens)
    else:
        raise ValueError("command must be a string or sequence of arguments")

    raw_env = raw.get("env")
    if isinstance(raw_env, Mapping):
        for k, v in raw_env.items():
            env_dict[str(k)] = str(v)
    elif isinstance(raw_env, (list, tuple)):
        for item in raw_env:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                env_dict[str(item[0])] = str(item[1])
            elif isinstance(item, str) and "=" in item:
                k, _, v = item.partition("=")
                env_dict[k] = v

    timeout_s = float(raw.get("timeout_s", DEFAULT_CHECK_TIMEOUT))
    scope = str(raw.get("scope", "unit_scoped")).strip()
    safety_tier = str(raw.get("safety_tier", "read_only")).strip()
    expected_exit_code = int(raw.get("expected_exit_code", 0))
    critical = bool(raw.get("critical", True))

    return VerificationCheck(
        check_id=check_id,
        command=cmd_tokens,
        timeout_s=timeout_s,
        scope=scope,
        safety_tier=safety_tier,
        expected_exit_code=expected_exit_code,
        critical=critical,
        env=tuple(sorted(env_dict.items())),
    )


def default_process_executor(
    argv: Sequence[str],
    timeout_s: float,
    workdir: Path,
    *,
    env: Mapping[str, str] | Sequence[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Execute command safely under isolated child process with bounded output."""
    start_t = time.monotonic()
    timed_out = False
    run_env = os.environ.copy()
    if env:
        if isinstance(env, Mapping):
            run_env.update({str(k): str(v) for k, v in env.items()})
        else:
            for k, v in env:
                run_env[str(k)] = str(v)

    try:
        proc = subprocess.Popen(
            list(argv),
            cwd=str(workdir),
            env=run_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            stdout, stderr = proc.communicate()
        duration = time.monotonic() - start_t

        stdout_str = stdout or ""
        stderr_str = stderr or ""
        if len(stdout_str.encode("utf-8")) > MAX_OUTPUT_BYTES:
            stdout_str = stdout_str[: MAX_OUTPUT_BYTES // 2] + "\n[TRUNCATED: output exceeded limit]"
        if len(stderr_str.encode("utf-8")) > MAX_OUTPUT_BYTES:
            stderr_str = stderr_str[: MAX_OUTPUT_BYTES // 2] + "\n[TRUNCATED: output exceeded limit]"

        return {
            "exit_code": 124 if timed_out else proc.returncode,
            "stdout": stdout_str,
            "stderr": stderr_str,
            "timed_out": timed_out,
            "duration_s": duration,
        }
    except Exception as exc:
        duration = time.monotonic() - start_t
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"ExecutionError: {type(exc).__name__}: {exc}",
            "timed_out": False,
            "duration_s": duration,
        }


def evaluate_verification_checks(
    checks: Sequence[Mapping[str, Any] | VerificationCheck],
    workdir: Path | str,
    *,
    executor: Callable[[Sequence[str], float, Path], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evaluate a sequence of verification checks against workdir with fail-closed semantics."""
    target_dir = Path(workdir).resolve()
    if not target_dir.is_dir():
        raise ValueError(f"workdir does not exist or is not a directory: {workdir}")

    if len(checks) > MAX_VERIFICATION_CHECKS:
        raise ValueError(f"number of checks ({len(checks)}) exceeds limit ({MAX_VERIFICATION_CHECKS})")

    exec_fn = executor or default_process_executor
    norm_checks = [normalize_verification_check(c) for c in checks]

    results: list[dict[str, Any]] = []
    halted_on_critical = False
    any_timed_out = False
    any_failed = False

    for idx, check in enumerate(norm_checks):
        if halted_on_critical:
            results.append({
                "check_id": check.check_id,
                "status": "skipped",
                "exit_code": None,
                "passed": False,
                "reason": "skipped_due_to_prior_critical_failure",
                "stdout_sha256": "",
                "stderr_sha256": "",
                "duration_s": 0.0,
            })
            continue

        try:
            raw_res = exec_fn(check.command, check.timeout_s, target_dir, env=dict(check.env))  # type: ignore
        except TypeError:
            raw_res = exec_fn(check.command, check.timeout_s, target_dir)
        exit_code = raw_res.get("exit_code")
        timed_out = bool(raw_res.get("timed_out"))
        stdout = str(raw_res.get("stdout", ""))
        stderr = str(raw_res.get("stderr", ""))
        duration = float(raw_res.get("duration_s", 0.0))

        stdout_digest = hashlib.sha256(stdout.encode("utf-8")).hexdigest()
        stderr_digest = hashlib.sha256(stderr.encode("utf-8")).hexdigest()

        if timed_out:
            status = "timed_out"
            passed = False
            any_timed_out = True
            any_failed = True
        elif exit_code == check.expected_exit_code:
            status = "passed"
            passed = True
        else:
            status = "failed"
            passed = False
            any_failed = True

        results.append({
            "check_id": check.check_id,
            "status": status,
            "exit_code": exit_code,
            "passed": passed,
            "stdout_sha256": stdout_digest,
            "stderr_sha256": stderr_digest,
            "duration_s": round(duration, 3),
        })

        if not passed and check.critical:
            halted_on_critical = True

    if not norm_checks:
        verdict = "approved"
    elif any_timed_out:
        verdict = "timed_out"
    elif halted_on_critical:
        verdict = "halted_on_critical"
    elif any_failed:
        verdict = "rejected"
    else:
        verdict = "approved"

    serialized = ":".join(f"{r['check_id']}={r['status']}" for r in results)
    receipt_id = f"vrec-{hashlib.sha256(serialized.encode('utf-8')).hexdigest()[:12]}"

    return {
        "schema_version": VERIFICATION_RECEIPT_SCHEMA,
        "receipt_id": receipt_id,
        "workdir": str(target_dir),
        "verdict": verdict,
        "all_passed": (verdict == "approved"),
        "critical_failed": halted_on_critical,
        "check_count": len(norm_checks),
        "results": results,
        "claim_boundary": (
            "A verification receipt records observed exit codes and output digests of contracted checks. "
            "It is not proof that the whole system is bug-free."
        ),
    }


def validate_verification_receipt(receipt: Mapping[str, Any] | object) -> list[str]:
    """Validate verification receipt structure."""
    if not isinstance(receipt, Mapping):
        return ["receipt must be a mapping"]

    errors: list[str] = []
    if receipt.get("schema_version") != VERIFICATION_RECEIPT_SCHEMA:
        errors.append("schema_version is invalid")
    if not str(receipt.get("receipt_id", "")).startswith("vrec-"):
        errors.append("receipt_id must start with vrec-")
    if receipt.get("verdict") not in {"approved", "rejected", "halted_on_critical", "timed_out"}:
        errors.append("verdict is invalid")
    if not isinstance(receipt.get("results"), (list, tuple)):
        errors.append("results must be a sequence")
    if "not" not in str(receipt.get("claim_boundary", "")).casefold():
        errors.append("claim_boundary must state a limitation")
    return errors
