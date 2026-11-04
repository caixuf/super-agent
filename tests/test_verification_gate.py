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


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf")])
def test_verification_timeout_must_be_finite(timeout):
    with pytest.raises(ValueError, match="timeout_s"):
        normalize_verification_check({"check_id": "bounded", "command": "true", "timeout_s": timeout})


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


def test_executor_internal_type_error_is_never_retried(tmp_path):
    calls = []

    def execute(argv, timeout_s, workdir, *, env=None):
        calls.append(env)
        with (workdir / "effects").open("a") as stream:
            stream.write("started\n")
        if env is not None:
            raise TypeError("internal failure after starting the command")
        return {"exit_code": 0}

    with pytest.raises(TypeError, match="internal failure"):
        evaluate_verification_checks(
            [{"check_id": "once", "command": "test"}], tmp_path, executor=execute)
    assert calls == [{}]
    assert (tmp_path / "effects").read_text() == "started\n"


def test_legacy_executor_cannot_silently_drop_required_environment(tmp_path):
    calls = []

    def execute(argv, timeout_s, workdir):
        calls.append(argv)
        return {"exit_code": 0}

    with pytest.raises(TypeError, match="verification_executor_env_required"):
        evaluate_verification_checks([
            {"check_id": "first", "command": "first"},
            {"check_id": "isolated", "command": "test", "env": {"ISOLATED": "1"}},
        ], tmp_path, executor=execute)
    assert not calls


def test_falsey_executor_is_not_replaced_by_host_process_executor(tmp_path):
    calls = []

    class Executor:
        def __bool__(self):
            return False

        def __call__(self, argv, timeout_s, workdir, *, env):
            calls.append(argv)
            return {"exit_code": 0}

    result = evaluate_verification_checks(
        [{"check_id": "isolated", "command": "not-a-host-program"}], tmp_path, executor=Executor())
    assert calls == [("not-a-host-program",)]
    assert result["all_passed"] is True


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

    res3 = default_process_executor(
        argv=["python3", "-c", "print('A' * 2_000_000)"],
        timeout_s=5.0,
        workdir=tmp_path,
    )
    assert "[TRUNCATED:" in res3["stdout"]
    assert len(res3["stdout"]) < 1_500_000


@pytest.mark.parametrize("channel", [1, 2])
def test_verifier_stops_at_output_budget_before_later_effects(tmp_path, monkeypatch, channel):
    import sys
    import agentcore.verification_gate as gate

    monkeypatch.setattr(gate, "MAX_OUTPUT_BYTES", 4096)
    result = gate.default_process_executor(
        [sys.executable, "-c",
         f"import os,time;os.write({channel},b'x'*16384);time.sleep(.4);open('late-effect','w').close()"],
        3, tmp_path,
    )
    assert result.get("output_limited") is True
    assert result["timed_out"] is False
    assert result["exit_code"] != 0
    assert not (tmp_path / "late-effect").exists()
    for stream in ("stdout", "stderr"):
        assert len(result[stream].encode("utf-8")) <= 4096 + 200


def test_output_limited_executor_result_never_approves_expected_exit_code(tmp_path):
    receipt = evaluate_verification_checks(
        [{"check_id": "bounded", "command": "run", "expected_exit_code": -9}], tmp_path,
        executor=lambda *args, **kwargs: {"exit_code": -9, "output_limited": True},
    )
    assert receipt["all_passed"] is False
    assert receipt["results"][0]["status"] == "error"


def test_spawn_error_does_not_pass_a_negative_expected_exit(tmp_path):
    receipt = evaluate_verification_checks(
        [{"check_id": "missing", "command": ["./not-installed"], "expected_exit_code": -1}],
        tmp_path,
    )
    assert receipt["all_passed"] is False
    assert receipt["results"][0]["status"] == "error"


def test_verifier_decodes_non_utf8_output_without_losing_exit_status(tmp_path):
    import sys
    from agentcore.verification_gate import default_process_executor

    result = default_process_executor(
        [sys.executable, "-c", "import os;os.write(1,b'\\xffhello');os.write(2,b'\\xfewarn')"],
        3, tmp_path,
    )
    assert result["exit_code"] == 0 and result["timed_out"] is False
    assert "hello" in result["stdout"] and "warn" in result["stderr"]


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), 0, -1])
def test_local_executor_rejects_invalid_timeout_before_spawn(tmp_path, timeout):
    import sys
    from agentcore.verification_gate import default_process_executor

    with pytest.raises(ValueError, match="positive_finite"):
        default_process_executor(
            [sys.executable, "-c", "open('unexpected-effect','w').close()"], timeout, tmp_path)
    assert not (tmp_path / "unexpected-effect").exists()


def test_verifier_timeout_still_applies_after_both_output_pipes_close(tmp_path):
    import sys
    import time
    from agentcore.verification_gate import default_process_executor

    started = time.monotonic()
    result = default_process_executor(
        [sys.executable, "-c", "import os,time;os.close(1);os.close(2);time.sleep(5)"],
        .1, tmp_path,
    )
    assert result["timed_out"] is True and result["exit_code"] == 124
    assert time.monotonic() - started < 1.5


def test_verifier_timeout_never_waits_for_detached_pipe_holder(tmp_path):
    import os
    import signal
    import sys
    import time
    from agentcore.verification_gate import default_process_executor

    marker = tmp_path / "pipe-holder"
    code = (
        "import subprocess,sys,time;from pathlib import Path;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(3)'],start_new_session=True);"
        "s=Path('/proc/%d/stat'%p.pid).read_text().rsplit(')',1)[1].split()[19];"
        "Path('pipe-holder').write_text(str(p.pid)+':'+s);time.sleep(10)"
    )
    started = time.monotonic()
    try:
        result = default_process_executor([sys.executable, "-c", code], .2, tmp_path)
        assert result["timed_out"] is True
        assert time.monotonic() - started < 1.5
        assert marker.is_file(), "detached pipe-holder premise must actually run"
    finally:
        if marker.exists():
            pid, expected_start = marker.read_text().split(":")
            try:
                actual_start = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
                if actual_start == expected_start:
                    os.kill(int(pid), signal.SIGKILL)
            except (OSError, IndexError):
                pass


def test_evaluate_verification_checks_real_end_to_end(tmp_path: Path):
    checks = [
        {"check_id": "c_env_real", "command": "TEST_VAR=passed python3 -c \"import os; assert os.environ['TEST_VAR'] == 'passed'\""},
    ]
    receipt = evaluate_verification_checks(checks, tmp_path)
    assert receipt["all_passed"] is True
    assert receipt["verdict"] == "approved"
    assert receipt["results"][0]["status"] == "passed"


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


def test_verify_code_execution_insufficient_passed_tests():
    gate = VerificationGate()
    rep = gate.verify_code_execution(
        task_id="code-insufficient",
        command="pytest tests/fast.py",
        exit_code=0,
        stdout_tail="2 passed in 0.05s",
        min_passed_tests=5,
    )
    assert rep.ok is False
    assert any("通过用例数不足" in f for f in rep.failed_assertions)
    with pytest.raises(VerificationGateError):
        gate.assert_evidence(rep)


def test_verify_log_evidence_invalid_regex():
    gate = VerificationGate()
    rep = gate.verify_log_evidence(
        task_id="log-invalid-re",
        log_path="/var/log/app.log",
        pattern=r"[unclosed character set",
        log_content="sample log",
    )
    assert rep.ok is False
    assert any("无效的正则表达式模式" in f for f in rep.failed_assertions)


def test_self_correction_loop_handles_verify_exception():
    loop = SelfCorrectionLoop(max_attempts=2)

    def execute_fn(attempt: int, repair_ctx: RepairContext | None):
        return {"attempt": attempt}

    def verify_fn(res):
        raise RuntimeError("verification service crashed")

    result = loop.run("exc-01", execute_fn, verify_fn)
    assert result.success is False
    assert result.total_attempts == 2
    assert "EXCEEDED_MAX_RETRY_LIMIT_2" in result.terminated_reason
    assert any("verification service crashed" in f for f in result.final_report.failed_assertions)

