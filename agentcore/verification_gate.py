"""Deterministic gate contracts, safe verification execution harnesses, and evidence-based termination.

Ported and adapted from open-source gate contracts and verification principles:
- Enforces strict contract schemas for executable verification checks.
- Runs checks under bounded timeouts with output truncation and digest extraction.
- Fails closed on critical failures to halt cascading corrupted executions.
- Supports reproduction checks (expecting non-zero exits) as well as regular tests.
- Issues tamper-evident, privacy-preserving verification receipts.

核心增强（四大进阶特性 3）：
1. 机器证据验证收尾门禁（Evidence-Based Termination）：
   - 彻底废除口头表述结案，凡代码改动必须附带真实退出码 0 与用例通过数；
   - 凡服务修改必须提供 systemd active (running) 或端口探针证据；
   - 凡硬件/日志排查必须给出实际行号或命令原文字符；
2. 失败自愈打回循环（Self-Correction Loop）：
   - 机器断言失败时自动提取真实错误堆栈，生成 RepairContext 修复上下文；
   - 自动打回重试（上限至多 3 轮）；
   - 超过 3 轮如实汇报故障断点，严禁伪造假绿。
"""
from __future__ import annotations

import enum
import hashlib
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
import re
import shlex
import signal
import subprocess
import time
from typing import Any, Callable, Dict, Final, List, Mapping, Optional, Sequence, Tuple, Union

logger = logging.getLogger(__name__)

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
    expected_exit = int(raw.get("expected_exit_code", 0))
    critical = bool(raw.get("critical", True))

    env_tuples = tuple(sorted(env_dict.items()))

    return VerificationCheck(
        check_id=check_id,
        command=cmd_tokens,
        timeout_s=timeout_s,
        scope=scope,
        safety_tier=safety_tier,
        expected_exit_code=expected_exit,
        critical=critical,
        env=env_tuples,
    )


def default_process_executor(
    argv: Sequence[str],
    timeout_s: float,
    workdir: Path,
    *,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Execute a verification command in a subprocess with bounded time and output."""
    t0 = time.monotonic()
    run_env = os.environ.copy()
    if env:
        run_env.update(env)

    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(workdir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=run_env,
            start_new_session=True,
        )
    except Exception as exc:
        duration = time.monotonic() - t0
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"SpawnError: {type(exc).__name__}: {exc}",
            "timed_out": False,
            "duration_s": duration,
        }

    try:
        stdout_str, stderr_str = proc.communicate(timeout=timeout_s)
        duration = time.monotonic() - t0
        if len(stdout_str.encode("utf-8")) > MAX_OUTPUT_BYTES:
            stdout_str = stdout_str[:MAX_OUTPUT_BYTES] + f"\n[TRUNCATED: output exceeded {MAX_OUTPUT_BYTES} bytes]"
        if len(stderr_str.encode("utf-8")) > MAX_OUTPUT_BYTES:
            stderr_str = stderr_str[:MAX_OUTPUT_BYTES] + f"\n[TRUNCATED: output exceeded {MAX_OUTPUT_BYTES} bytes]"
        return {
            "exit_code": proc.returncode,
            "stdout": stdout_str,
            "stderr": stderr_str,
            "timed_out": False,
            "duration_s": duration,
        }
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - t0
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            pass
        proc.communicate()
        return {
            "exit_code": 124,
            "stdout": "",
            "stderr": f"Command timed out after {timeout_s}s",
            "timed_out": True,
            "duration_s": duration,
        }
    except Exception as exc:
        duration = time.monotonic() - t0
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


# =========================================================================
# 进阶机器证据验证门禁与失败自愈循环
# =========================================================================

class EvidenceType(str, enum.Enum):
    CODE_TEST = "code_test"
    SERVICE_STATUS = "service_status"
    LOG_ASSERTION = "log_assertion"
    CUSTOM = "custom"


@dataclass
class VerificationEvidence:
    evidence_type: EvidenceType = EvidenceType.CUSTOM
    passed: bool = True
    summary: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass
class CodeVerificationEvidence(VerificationEvidence):
    evidence_type: EvidenceType = EvidenceType.CODE_TEST
    command: str = ""
    exit_code: int = -1
    passed_tests: int = 0
    failed_tests: int = 0
    raw_output_snippet: str = ""


@dataclass
class ServiceVerificationEvidence(VerificationEvidence):
    evidence_type: EvidenceType = EvidenceType.SERVICE_STATUS
    service_name: str = ""
    active_status: str = ""
    port: Optional[int] = None
    probe_http_status: Optional[int] = None


@dataclass
class LogAssertionEvidence(VerificationEvidence):
    evidence_type: EvidenceType = EvidenceType.LOG_ASSERTION
    log_path: str = ""
    pattern: str = ""
    matched_content: str = ""
    line_number: Optional[int] = None


@dataclass
class VerificationReport:
    task_id: str
    task_type: str
    ok: bool
    passed_assertions: List[str]
    failed_assertions: List[str]
    evidences: List[Dict[str, Any]]
    error_summary: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_markdown(self) -> str:
        status_label = "[PASS]" if self.ok else "[FAIL]"
        lines = [
            f"### {status_label} 机器验证收尾报告: {self.task_id}",
            f"- 任务类型: {self.task_type}",
            f"- 验收结论: {'全部断言通过' if self.ok else '门禁拦截，存在未满足项'}",
        ]
        if self.passed_assertions:
            lines.append("- 通过的机器断言:")
            for p in self.passed_assertions:
                lines.append(f"  + [OK] {p}")
        if self.failed_assertions:
            lines.append("- 失败的机器断言:")
            for f in self.failed_assertions:
                lines.append(f"  * [FAIL] {f}")
        if self.error_summary:
            lines.append(f"- 故障诊断摘要: {self.error_summary}")
        return "\n".join(lines)


class VerificationGateError(RuntimeError):
    """机器验证门禁拦截异常"""
    def __init__(self, message: str, report: VerificationReport) -> None:
        super().__init__(message)
        self.report = report


class VerificationGate:
    """机器证据验证门禁管理器"""

    def verify_code_execution(
        self,
        task_id: str,
        command: str,
        exit_code: int,
        stdout_tail: str = "",
        min_passed_tests: int = 1,
    ) -> VerificationReport:
        """验证代码/测试运行结果"""
        passed_assertions: List[str] = []
        failed_assertions: List[str] = []

        if exit_code == 0:
            passed_assertions.append(f"命令退出码为 0: `{command}`")
        else:
            failed_assertions.append(f"命令非零退出 (exit_code={exit_code}): `{command}`")

        passed_count = 0
        failed_count = 0
        m_pass = re.search(r"(\d+)\s+passed", stdout_tail)
        if m_pass:
            passed_count = int(m_pass.group(1))
        m_fail = re.search(r"(\d+)\s+failed", stdout_tail)
        if m_fail:
            failed_count = int(m_fail.group(1))

        if failed_count > 0:
            failed_assertions.append(f"测试套件出现 {failed_count} 项失败")
        elif exit_code == 0 and passed_count >= min_passed_tests:
            passed_assertions.append(f"测试套件全部通过 ({passed_count} 项 passed)")

        is_ok = len(failed_assertions) == 0 and exit_code == 0
        err_msg = "; ".join(failed_assertions) if not is_ok else ""

        ev = CodeVerificationEvidence(
            passed=is_ok,
            summary=f"代码测试结果: exit_code={exit_code}, passed={passed_count}, failed={failed_count}",
            command=command,
            exit_code=exit_code,
            passed_tests=passed_count,
            failed_tests=failed_count,
            raw_output_snippet=stdout_tail[-500:],
        )

        return VerificationReport(
            task_id=task_id,
            task_type="code_test",
            ok=is_ok,
            passed_assertions=passed_assertions,
            failed_assertions=failed_assertions,
            evidences=[asdict(ev)],
            error_summary=err_msg,
        )

    def verify_service_status(
        self,
        task_id: str,
        service_name: str,
        active_status: str,
        port_listening: Optional[bool] = None,
        http_probe_code: Optional[int] = None,
    ) -> VerificationReport:
        """验证系统服务与端口探针"""
        passed_assertions: List[str] = []
        failed_assertions: List[str] = []

        if active_status.lower() in ("active", "running", "active (running)"):
            passed_assertions.append(f"服务处于活跃运行态: {service_name} ({active_status})")
        else:
            failed_assertions.append(f"服务状态异常: {service_name} (status={active_status})")

        if port_listening is not None:
            if port_listening:
                passed_assertions.append(f"服务端口正在正常监听")
            else:
                failed_assertions.append(f"服务端口未见监听")

        if http_probe_code is not None:
            if 200 <= http_probe_code < 400:
                passed_assertions.append(f"HTTP 探针返回成功码 {http_probe_code}")
            else:
                failed_assertions.append(f"HTTP 探针返回异常状态码 {http_probe_code}")

        is_ok = len(failed_assertions) == 0
        err_msg = "; ".join(failed_assertions) if not is_ok else ""

        ev = ServiceVerificationEvidence(
            passed=is_ok,
            summary=f"服务状态: {service_name} -> {active_status}",
            service_name=service_name,
            active_status=active_status,
            probe_http_status=http_probe_code,
        )

        return VerificationReport(
            task_id=task_id,
            task_type="service_status",
            ok=is_ok,
            passed_assertions=passed_assertions,
            failed_assertions=failed_assertions,
            evidences=[asdict(ev)],
            error_summary=err_msg,
        )

    def verify_log_evidence(
        self,
        task_id: str,
        log_path: str,
        pattern: str,
        log_content: str,
    ) -> VerificationReport:
        """验证排查日志中包含的关键事实特征与行号"""
        passed_assertions: List[str] = []
        failed_assertions: List[str] = []
        matched_snippet = ""
        matched_line_no = None

        lines = log_content.splitlines()
        for i, line in enumerate(lines, start=1):
            if re.search(pattern, line):
                matched_snippet = line.strip()
                matched_line_no = i
                break

        if matched_snippet:
            passed_assertions.append(f"日志 `{log_path}` 第 {matched_line_no} 行命中特征: `{matched_snippet[:80]}`")
        else:
            failed_assertions.append(f"日志 `{log_path}` 未找到预期特征: `{pattern}`")

        is_ok = len(failed_assertions) == 0
        err_msg = "; ".join(failed_assertions) if not is_ok else ""

        ev = LogAssertionEvidence(
            passed=is_ok,
            summary=f"日志特征核验: {log_path} -> {'HIT' if is_ok else 'MISS'}",
            log_path=log_path,
            pattern=pattern,
            matched_content=matched_snippet,
            line_number=matched_line_no,
        )

        return VerificationReport(
            task_id=task_id,
            task_type="log_assertion",
            ok=is_ok,
            passed_assertions=passed_assertions,
            failed_assertions=failed_assertions,
            evidences=[asdict(ev)],
            error_summary=err_msg,
        )

    def assert_evidence(self, report: VerificationReport) -> None:
        """门禁断言铁律：未提供有效证据或验证失败，直接拦截并拒绝结案。"""
        if not report.ok:
            raise VerificationGateError(
                f"[GATE_REJECTED] 机器断言验证未通过: {report.error_summary}",
                report=report,
            )


@dataclass
class RepairContext:
    attempt: int
    max_attempts: int
    last_report: VerificationReport
    error_summary: str
    suggested_action: str = ""


@dataclass
class SelfHealingResult:
    success: bool
    total_attempts: int
    final_report: VerificationReport
    attempt_history: List[Dict[str, Any]]
    terminated_reason: str


class SelfCorrectionLoop:
    """至多 3 轮失败自愈打回循环"""

    def __init__(self, max_attempts: int = 3) -> None:
        self.max_attempts = max(1, min(max_attempts, 5))

    def run(
        self,
        task_id: str,
        execute_fn: Callable[[int, Optional[RepairContext]], Any],
        verify_fn: Callable[[Any], VerificationReport],
    ) -> SelfHealingResult:
        """驱动执行与门禁验证，遇到失败提取堆栈并自动重试（至多 3 轮）。"""
        history: List[Dict[str, Any]] = []
        repair_context: Optional[RepairContext] = None
        last_report: Optional[VerificationReport] = None

        for attempt in range(1, self.max_attempts + 1):
            t0 = time.time()
            try:
                action_result = execute_fn(attempt, repair_context)
            except Exception as exec_err:
                action_result = None
                report = VerificationReport(
                    task_id=task_id,
                    task_type="execution_exception",
                    ok=False,
                    passed_assertions=[],
                    failed_assertions=[f"执行过程发生异常: {exec_err}"],
                    evidences=[],
                    error_summary=str(exec_err),
                )
            else:
                report = verify_fn(action_result)

            duration = time.time() - t0
            last_report = report
            history.append({
                "attempt": attempt,
                "ok": report.ok,
                "duration_s": round(duration, 3),
                "error_summary": report.error_summary,
                "passed_count": len(report.passed_assertions),
                "failed_count": len(report.failed_assertions),
            })

            if report.ok:
                logger.info("[SelfCorrectionLoop] 任务在第 %d 轮成功通过机器验证门禁", attempt)
                return SelfHealingResult(
                    success=True,
                    total_attempts=attempt,
                    final_report=report,
                    attempt_history=history,
                    terminated_reason="ALL_CRITERIA_MET",
                )

            logger.warning(
                "[SelfCorrectionLoop] 第 %d/%d 轮验证未通过: %s",
                attempt, self.max_attempts, report.error_summary
            )

            if attempt < self.max_attempts:
                repair_context = RepairContext(
                    attempt=attempt,
                    max_attempts=self.max_attempts,
                    last_report=report,
                    error_summary=report.error_summary,
                    suggested_action=f"修复上一轮失败断言: {'; '.join(report.failed_assertions)}",
                )

        return SelfHealingResult(
            success=False,
            total_attempts=self.max_attempts,
            final_report=last_report,
            attempt_history=history,
            terminated_reason=f"EXCEEDED_MAX_RETRY_LIMIT_{self.max_attempts}",
        )
