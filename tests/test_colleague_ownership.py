"""Unit tests for Raven-style Role Path Ownership & Access Enforcement."""
from __future__ import annotations

from pathlib import Path
import pytest

from agentcore.colleague_dev import (
    DEV_ROLES,
    HARD,
    SOFT,
    RolePathOwnership,
    assert_role_path_access,
    get_role_ownership,
    validate_role_path_access,
)


def test_canonical_roles_have_ownership_spec():
    for role_name in DEV_ROLES:
        ownership = get_role_ownership(role_name)
        assert isinstance(ownership, RolePathOwnership)
        assert ownership.role == role_name
        assert len(ownership.owns) > 0
        assert len(ownership.never) > 0
        assert ownership.enforce_write == HARD


def test_architect_ownership_rules():
    # Architect owns docs and specs
    allowed, reason = validate_role_path_access("architect", "docs/architecture.md", "write")
    assert allowed is True
    assert "allowed_by_owns" in reason

    allowed, _ = validate_role_path_access("architect", "SPEC.md", "write")
    assert allowed is True

    # Architect appends to decisions
    allowed, reason = validate_role_path_access("architect", "DECISIONS.md", "append")
    assert allowed is True
    assert "allowed_by_appends" in reason

    # Architect cannot write/overwrite append-only file
    allowed, reason = validate_role_path_access("architect", "DECISIONS.md", "write")
    assert allowed is False
    assert "append_only_path" in reason

    # Architect is strictly forbidden from touching code or tests
    allowed, reason = validate_role_path_access("architect", "src/main.py", "write")
    assert allowed is False
    assert "path_in_never_list" in reason

    allowed, reason = validate_role_path_access("architect", "tests/test_core.py", "write")
    assert allowed is False
    assert "path_in_never_list" in reason


def test_cases_and_test_ownership_separation():
    # Cases role owns src and case_set
    allowed, _ = validate_role_path_access("cases", "src/core.py", "write")
    assert allowed is True
    allowed, _ = validate_role_path_access("cases", "case_set/default/test_01.dsl", "write")
    assert allowed is True

    # Cases role cannot touch docs/specs
    allowed, reason = validate_role_path_access("cases", "docs/specs/spec.md", "write")
    assert allowed is False
    assert "path_in_never_list" in reason

    # Test role owns tests and test_reports
    allowed, _ = validate_role_path_access("test", "tests/unit/test_api.py", "write")
    assert allowed is True
    allowed, _ = validate_role_path_access("test", "test_reports/summary.json", "write")
    assert allowed is True

    # Test role cannot mutate source code directly (must not fake test passing)
    allowed, reason = validate_role_path_access("test", "src/api.py", "write")
    assert allowed is False
    assert "path_in_never_list" in reason


def test_reviewer_is_read_only_on_source():
    # Reviewer owns reports and reviews
    allowed, _ = validate_role_path_access("reviewer", "reviews/20261109-report.md", "write")
    assert allowed is True

    # Reviewer cannot modify src or tests directly
    allowed, reason = validate_role_path_access("reviewer", "src/module.py", "write")
    assert allowed is False
    assert "path_in_never_list" in reason

    allowed, reason = validate_role_path_access("reviewer", "tests/test_module.py", "write")
    assert allowed is False
    assert "path_in_never_list" in reason


def test_assert_role_path_access_raises():
    with pytest.raises(PermissionError, match="role_ownership_violation"):
        assert_role_path_access("architect", "src/critical.py", "write")

    # Should not raise for valid path
    assert_role_path_access("architect", "docs/SPEC.md", "write")


def test_absolute_path_with_root(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "docs" / "api.md"
    target.parent.mkdir()

    allowed, _ = validate_role_path_access("architect", target, "write", root=root)
    assert allowed is True

    forbidden = root / "src" / "core.py"
    allowed, _ = validate_role_path_access("architect", forbidden, "write", root=root)
    assert allowed is False

