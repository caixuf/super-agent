"""Unit tests for agentcore.verification_gate (contract schemas, evidence verification & self-healing)."""
from __future__ import annotations

from pathlib import Path
import pytest

from agentcore.verification_gate import (
    MAX_VERIFICATION_CHECKS,
    MAX_VERIFICATION_COMMAND_CHARS,
    VERIFICATION_CHECK_CONTRACT_SCHEMA,
    VERIFICATION_RECEIPT_SCHEMA,
    CodeVerificationEvidence,
    LogAssertionEvidence,
    RepairContext,
    SelfCorrectionLoop,
    ServiceVerificationEvidence,
    VerificationCheck,
    VerificationGate,
    VerificationGateError,
    VerificationReport,
    evaluate_verification_checks,
    normalize_verification_check,
    validate_verification_receipt,
)


def test_verification_check_initialization():
    check = VerificationCheck(
        check_id="syntax_check",
        command=("python3", "-m", "py_compile", "main.py"),
        timeout_s=15.0,
        scope="unit_scoped",
        safety_tier="read_only",
        expected_exit_code=0,
        critical=True,
    )
    assert check.check_id == "syntax_check"
    assert check.command == ("python3", "-m", "py_compile", "main.py")
    assert check.critical is True


def test_verification_check_validation_errors():
    with pytest.raises(ValueError, match="invalid check_id"):
        VerificationCheck(check_id="bad id with spaces", command=("ls",))

    with pytest.raises(ValueError, match="cannot be empty"):
        VerificationCheck(check_id="c1", command=())

    with pytest.raises(ValueError, match="exceeds cap"):
        long_arg = "a" * (MAX_VERIFICATION_COMMAND_CHARS + 10)
        VerificationCheck(check_id="c2", command=("echo", long_arg))

    with pytest.raises(ValueError, match="timeout_s"):
        VerificationCheck(check_id="c3", command=("ls",), timeout_s=-5.0)

    with pytest.raises(ValueError, match="scope"):
        VerificationCheck(check_id="c4", command=("ls",), scope="global_scoped")

    with pytest.raises(ValueError, match="safety_tier"):
        VerificationCheck(check_id="c5", command=("ls",), safety_tier="dangerous")


def test_normalize_verification_check():
    raw1 = {
        "check_id": "lint",
        "command": "ruff check .",
        "timeout_s": 20,
        "critical": False,
    }
    norm1 = normalize_verification_check(raw1)
    assert norm1.check_id == "lint"
    assert norm1.command == ("ruff", "check", ".")
    assert norm1.critical is False

    norm2 = normalize_verification_check(norm1)
    assert norm2 is norm1

    with pytest.raises(TypeError, match="Mapping"):
        normalize_verification_check(123)  # type: ignore


def test_evaluate_verification_checks_all_passed(tmp_path: Path):
    checks = [
        {"check_id": "c1", "command": "echo hello", "expected_exit_code": 0},
        {"check_id": "c2", "command": "echo world", "expected_exit_code": 0},
    ]

    def mock_executor(argv, timeout_s, workdir):
        return {
            "exit_code": 0,
            "stdout": f"output of {' '.join(argv)}",
            "stderr": "",
            "timed_out": False,
            "duration_s": 0.05,
        }

    receipt = evaluate_verification_checks(checks, tmp_path, executor=mock_executor)

    assert receipt["schema_version"] == VERIFICATION_RECEIPT_SCHEMA
    assert receipt["receipt_id"].startswith("vrec-")
    assert receipt["all_passed"] is True
    assert receipt["verdict"] == "approved"
    assert receipt["critical_failed"] is False
    assert len(receipt["results"]) == 2
    assert receipt["results"][0]["status"] == "passed"
    assert len(receipt["results"][0]["stdout_sha256"]) == 64
    assert validate_verification_receipt(receipt) == []


def test_evaluate_verification_checks_critical_halts_remaining(tmp_path: Path):
    checks = [
        {"check_id": "c1", "command": "exit_1", "expected_exit_code": 0, "critical": True},
        {"check_id": "c2", "command": "echo after", "expected_exit_code": 0},
    ]

    def mock_executor(argv, timeout_s, workdir):
        if argv[0] == "exit_1":
            return {"exit_code": 1, "stdout": "", "stderr": "failed", "timed_out": False, "duration_s": 0.01}
        return {"exit_code": 0, "stdout": "ok", "stderr": "", "timed_out": False, "duration_s": 0.01}

    receipt = evaluate_verification_checks(checks, tmp_path, executor=mock_executor)

    assert receipt["all_passed"] is False
    assert receipt["critical_failed"] is True
    assert receipt["verdict"] == "halted_on_critical"
    assert len(receipt["results"]) == 2
    assert receipt["results"][0]["status"] == "failed"
    assert receipt["results"][1]["status"] == "skipped"
    assert receipt["results"][1]["reason"] == "skipped_due_to_prior_critical_failure"


def test_evaluate_verification_checks_reproduction_expected_nonzero(tmp_path: Path):
    checks = [
        {"check_id": "repro_bug", "command": "run_reproduction", "expected_exit_code": 1, "critical": True},
    ]

    def mock_executor(argv, timeout_s, workdir):
        return {"exit_code": 1, "stdout": "AssertionError reproduced", "stderr": "", "timed_out": False, "duration_s": 0.02}

    receipt = evaluate_verification_checks(checks, tmp_path, executor=mock_executor)
    assert receipt["all_passed"] is True
    assert receipt["verdict"] == "approved"
    assert receipt["results"][0]["status"] == "passed"


def test_default_process_executor_real_process(tmp_path: Path):
    from agentcore.verification_gate import default_process_executor

    res1 = default_process_executor(
        argv=["python3", "-c", "import os; assert os.environ.get('MY_FLAG') == 'hello'"],
        timeout_s=5.0,
        workdir=tmp_path,
        env={"MY_FLAG": "hello"},
    )
    assert res1["exit_code"] == 0
    assert res1["timed_out"] is False

    res2 = default_process_executor(
        argv=["python3", "-c", "import time; time.sleep(10)"],
        timeout_s=0.2,
        workdir=tmp_path,
    )
    assert res2["timed_out"] is True
    assert res2["exit_code"] == 124


# --- 进阶证据门禁与自愈测试 ---

def test_verify_code_execution_pass():
    gate = VerificationGate()
    report = gate.verify_code_execution(
        task_id="code-001",
        command="pytest tests/test_entity_recall.py -q",
        exit_code=0,
        stdout_tail="7 passed in 0.26s",
        min_passed_tests=1,
    )
    assert report.ok is True
    assert len(report.passed_assertions) >= 2
    assert report.failed_assertions == []
    gate.assert_evidence(report)


def test_verify_code_execution_fail_on_exit_code():
    gate = VerificationGate()
    report = gate.verify_code_execution(
        task_id="code-002",
        command="pytest tests/broken.py",
        exit_code=1,
        stdout_tail="2 failed, 3 passed",
    )
    assert report.ok is False
    assert any("非零退出" in f for f in report.failed_assertions)
    assert any("2 项失败" in f for f in report.failed_assertions)

    with pytest.raises(VerificationGateError) as exc_info:
        gate.assert_evidence(report)
    assert "[GATE_REJECTED]" in str(exc_info.value)


def test_verify_service_status():
    gate = VerificationGate()
    rep_ok = gate.verify_service_status(
        task_id="svc-001",
        service_name="hermes-gateway-caixuf.service",
        active_status="active (running)",
        port_listening=True,
        http_probe_code=200,
    )
    assert rep_ok.ok is True
    assert len(rep_ok.passed_assertions) == 3

    rep_fail = gate.verify_service_status(
        task_id="svc-002",
        service_name="bge-m3-sidecar.service",
        active_status="failed",
        port_listening=False,
        http_probe_code=502,
    )
    assert rep_fail.ok is False
    assert len(rep_fail.failed_assertions) == 3


def test_verify_log_evidence():
    gate = VerificationGate()
    raw_logs = (
        "2026-10-31 10:00:00 INFO Initializing worker\n"
        "2026-10-31 10:00:05 ERROR GPU bus error Xid 79 fallen off bus\n"
        "2026-10-31 10:00:10 WARN Reconnection attempt failed\n"
    )
    rep_ok = gate.verify_log_evidence(
        task_id="log-001",
        log_path="/var/log/dmesg",
        pattern=r"Xid 79",
        log_content=raw_logs,
    )
    assert rep_ok.ok is True
    assert any("第 2 行命中特征" in p for p in rep_ok.passed_assertions)

    rep_fail = gate.verify_log_evidence(
        task_id="log-002",
        log_path="/var/log/dmesg",
        pattern=r"Kernel Panic",
        log_content=raw_logs,
    )
    assert rep_fail.ok is False
    assert any("未找到预期特征" in f for f in rep_fail.failed_assertions)


def test_self_correction_loop_heals_on_second_round():
    loop = SelfCorrectionLoop(max_attempts=3)
    attempts_seen = []

    def execute_fn(attempt: int, repair_ctx: RepairContext | None):
        attempts_seen.append(attempt)
        if attempt == 1:
            return {"exit_code": 1, "out": "1 failed"}
        return {"exit_code": 0, "out": "5 passed in 0.1s"}

    def verify_fn(res):
        gate = VerificationGate()
        return gate.verify_code_execution("heal-01", "test_cmd", res["exit_code"], res["out"])

    result = loop.run("heal-01", execute_fn, verify_fn)
    assert result.success is True
    assert result.total_attempts == 2
    assert attempts_seen == [1, 2]
    assert result.terminated_reason == "ALL_CRITERIA_MET"


def test_self_correction_loop_exceeds_max_attempts():
    loop = SelfCorrectionLoop(max_attempts=3)
    attempts_seen = []

    def execute_fn(attempt: int, repair_ctx: RepairContext | None):
        attempts_seen.append(attempt)
        return {"exit_code": 1, "out": "always failing test"}

    def verify_fn(res):
        gate = VerificationGate()
        return gate.verify_code_execution("fail-01", "cmd", res["exit_code"], res["out"])

    result = loop.run("fail-01", execute_fn, verify_fn)
    assert result.success is False
    assert result.total_attempts == 3
    assert attempts_seen == [1, 2, 3]
    assert "EXCEEDED_MAX_RETRY_LIMIT_3" in result.terminated_reason
    assert not result.final_report.ok
