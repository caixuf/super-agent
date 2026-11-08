"""Unit tests for Raven-style WHY failure taxonomy and causal diagnostics."""
from __future__ import annotations

import pytest

from agentcore.failure_mender import (
    WHY_CLASSES,
    decide_failure,
    diagnose_failure_why,
)


def test_why_classes_roster():
    expected = {
        "STOPPED_BEFORE_VERIFY",
        "PATH_OWNERSHIP_VIOLATION",
        "SPECIFICATION_MISMATCH",
        "TIMEOUT_WITHOUT_PROGRESS",
        "SYNTAX_OR_IMPORT_ERROR",
        "FLAKY_RESOURCE",
        "UNCLASSIFIED",
    }
    assert expected.issubset(set(WHY_CLASSES))


def test_diagnose_path_ownership_violation():
    err = "PermissionError: role_ownership_violation: role 'architect' cannot write 'src/main.py'"
    diag = diagnose_failure_why(err)
    assert diag["why"] == "PATH_OWNERSHIP_VIOLATION"
    assert "ownership" in diag["explanation"].lower()
    assert diag["remediation_code"] == "R_ENFORCE_PATH_OWNERSHIP"


def test_diagnose_stopped_before_verify():
    diag = diagnose_failure_why("Turn completed without assertions", context={"verification_run": False})
    assert diag["why"] == "STOPPED_BEFORE_VERIFY"
    assert diag["remediation_code"] == "R_MANDATORY_VERIFICATION"


def test_diagnose_specification_mismatch():
    err = "verification_checks_failed: verdict=assertion_failed"
    diag = diagnose_failure_why(err)
    assert diag["why"] == "SPECIFICATION_MISMATCH"
    assert diag["remediation_code"] == "R_REPLAN_SPEC_ALIGNMENT"


def test_diagnose_syntax_and_import_error():
    err = "SyntaxError: invalid syntax in module.py line 42"
    diag = diagnose_failure_why(err)
    assert diag["why"] == "SYNTAX_OR_IMPORT_ERROR"
    assert diag["remediation_code"] == "R_STATIC_SYNTAX_FIX"

    err2 = "ModuleNotFoundError: No module named 'non_existent_pkg'"
    diag2 = diagnose_failure_why(err2)
    assert diag2["why"] == "SYNTAX_OR_IMPORT_ERROR"


def test_diagnose_timeout():
    err = "TimeoutError: execution timed out after 900.0s"
    diag = diagnose_failure_why(err)
    assert diag["why"] == "TIMEOUT_WITHOUT_PROGRESS"
    assert diag["remediation_code"] == "R_EXTEND_BUDGET_OR_SPLIT"


def test_diagnose_flaky_resource():
    err = "ConnectionError: connection reset by peer"
    diag = diagnose_failure_why(err)
    assert diag["why"] == "FLAKY_RESOURCE"
    assert diag["remediation_code"] == "R_TRANSIENT_RETRY"


def test_decide_failure_includes_why_diagnosis():
    decision = decide_failure("role_ownership_violation on path", attempt=0, max_retries=2)
    assert "why_diagnosis" in decision
    assert decision["why_diagnosis"]["why"] == "PATH_OWNERSHIP_VIOLATION"

