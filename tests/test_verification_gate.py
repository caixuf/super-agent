"""Unit tests for agentcore.verification_gate."""
from __future__ import annotations

from pathlib import Path
import pytest

from agentcore.verification_gate import (
    MAX_VERIFICATION_CHECKS,
    MAX_VERIFICATION_COMMAND_CHARS,
    VERIFICATION_CHECK_CONTRACT_SCHEMA,
    VERIFICATION_RECEIPT_SCHEMA,
    VerificationCheck,
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
    # Invalid check_id
    with pytest.raises(ValueError, match="invalid check_id"):
        VerificationCheck(check_id="bad id with spaces", command=("ls",))

    # Empty command
    with pytest.raises(ValueError, match="cannot be empty"):
        VerificationCheck(check_id="c1", command=())

    # Exceeding command length cap
    with pytest.raises(ValueError, match="exceeds cap"):
        long_arg = "a" * (MAX_VERIFICATION_COMMAND_CHARS + 10)
        VerificationCheck(check_id="c2", command=("echo", long_arg))

    # Invalid timeout
    with pytest.raises(ValueError, match="timeout_s"):
        VerificationCheck(check_id="c3", command=("ls",), timeout_s=-5.0)

    # Invalid scope
    with pytest.raises(ValueError, match="scope"):
        VerificationCheck(check_id="c4", command=("ls",), scope="global_scoped")

    # Invalid safety tier
    with pytest.raises(ValueError, match="safety_tier"):
        VerificationCheck(check_id="c5", command=("ls",), safety_tier="dangerous")


def test_normalize_verification_check():
    # From Mapping with string command
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

    # From VerificationCheck instance
    norm2 = normalize_verification_check(norm1)
    assert norm2 is norm1

    # Invalid type
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


def test_evaluate_verification_checks_non_critical_failure(tmp_path: Path):
    checks = [
        {"check_id": "c1", "command": "exit_1", "expected_exit_code": 0, "critical": False},
        {"check_id": "c2", "command": "echo success", "expected_exit_code": 0, "critical": True},
    ]

    def mock_executor(argv, timeout_s, workdir):
        if argv[0] == "exit_1":
            return {"exit_code": 1, "stdout": "", "stderr": "warn", "timed_out": False, "duration_s": 0.01}
        return {"exit_code": 0, "stdout": "ok", "stderr": "", "timed_out": False, "duration_s": 0.01}

    receipt = evaluate_verification_checks(checks, tmp_path, executor=mock_executor)

    assert receipt["all_passed"] is False
    assert receipt["critical_failed"] is False
    assert receipt["verdict"] == "rejected"
    assert receipt["results"][0]["status"] == "failed"
    assert receipt["results"][1]["status"] == "passed"


def test_evaluate_verification_checks_reproduction_expected_nonzero(tmp_path: Path):
    # A reproduction check expects exit code 1 to prove a failure reproduces!
    checks = [
        {"check_id": "repro_bug", "command": "run_reproduction", "expected_exit_code": 1, "critical": True},
    ]

    def mock_executor(argv, timeout_s, workdir):
        return {"exit_code": 1, "stdout": "AssertionError reproduced", "stderr": "", "timed_out": False, "duration_s": 0.02}

    receipt = evaluate_verification_checks(checks, tmp_path, executor=mock_executor)
    assert receipt["all_passed"] is True
    assert receipt["verdict"] == "approved"
    assert receipt["results"][0]["status"] == "passed"


def test_evaluate_verification_checks_timeout(tmp_path: Path):
    checks = [{"check_id": "c_slow", "command": "sleep 100", "timeout_s": 1.0}]

    def mock_executor(argv, timeout_s, workdir):
        return {"exit_code": 124, "stdout": "", "stderr": "timed out", "timed_out": True, "duration_s": 1.0}

    receipt = evaluate_verification_checks(checks, tmp_path, executor=mock_executor)
    assert receipt["verdict"] == "timed_out"
    assert receipt["results"][0]["status"] == "timed_out"


def test_evaluate_verification_checks_limits(tmp_path: Path):
    too_many = [{"check_id": f"c_{i}", "command": "echo 1"} for i in range(MAX_VERIFICATION_CHECKS + 1)]
    with pytest.raises(ValueError, match="exceeds limit"):
        evaluate_verification_checks(too_many, tmp_path)

    non_existent = tmp_path / "does_not_exist"
    with pytest.raises(ValueError, match="does not exist"):
        evaluate_verification_checks([], non_existent)


def test_normalize_verification_check_with_env():
    raw = {
        "check_id": "c_env",
        "command": "PYTHONPATH=. FOO=bar python3 -c 'exit(0)'",
        "env": {"EXTRA": "1"},
    }
    check = normalize_verification_check(raw)
    assert check.command == ("python3", "-c", "exit(0)")
    env_dict = dict(check.env)
    assert env_dict["PYTHONPATH"] == "."
    assert env_dict["FOO"] == "bar"
    assert env_dict["EXTRA"] == "1"


def test_default_process_executor_real_process(tmp_path: Path):
    from agentcore.verification_gate import default_process_executor

    # 1. Success execution with env
    res1 = default_process_executor(
        argv=["python3", "-c", "import os; assert os.environ.get('MY_FLAG') == 'hello'"],
        timeout_s=5.0,
        workdir=tmp_path,
        env={"MY_FLAG": "hello"},
    )
    assert res1["exit_code"] == 0
    assert res1["timed_out"] is False

    # 2. Timeout and process group termination
    res2 = default_process_executor(
        argv=["python3", "-c", "import time; time.sleep(10)"],
        timeout_s=0.2,
        workdir=tmp_path,
    )
    assert res2["timed_out"] is True
    assert res2["exit_code"] == 124

    # 3. Output truncation
    res3 = default_process_executor(
        argv=["python3", "-c", "print('A' * 2_000_000)"],
        timeout_s=5.0,
        workdir=tmp_path,
    )
    assert "[TRUNCATED:" in res3["stdout"]
    assert len(res3["stdout"]) < 1_500_000


def test_evaluate_verification_checks_real_end_to_end(tmp_path: Path):
    # Runs without mock executor using real default_process_executor
    checks = [
        {"check_id": "c_env_real", "command": "TEST_VAR=passed python3 -c \"import os; assert os.environ['TEST_VAR'] == 'passed'\""},
    ]
    receipt = evaluate_verification_checks(checks, tmp_path)
    assert receipt["all_passed"] is True
    assert receipt["verdict"] == "approved"
    assert receipt["results"][0]["status"] == "passed"

