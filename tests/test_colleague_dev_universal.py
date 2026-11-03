"""Tests for Universal Engineering Colleague Team roles and business overrides."""
from __future__ import annotations

import pytest

from agentcore.colleague_dev import (
    DEV_ROLES,
    get_dev_roles,
    get_role_contract_markdown,
    register_role_override,
    reset_role_overrides,
)


def test_universal_roles_structure():
    roles = get_dev_roles()
    expected_canonical_roles = {
        "architect", "cases", "reviewer", "test",
        "triage", "backend", "frontend", "ops",
    }
    assert expected_canonical_roles.issubset(roles.keys())

    for role_name in expected_canonical_roles:
        role_def = roles[role_name]
        assert "display_name" in role_def and len(role_def["display_name"]) > 0
        assert "duty" in role_def and len(role_def["duty"]) > 0
        assert "can_do" in role_def and len(role_def["can_do"]) >= 2
        assert "boundaries" in role_def and len(role_def["boundaries"]) >= 2

        contract_md = get_role_contract_markdown(role_name)
        assert role_def["display_name"] in contract_md
        assert role_def["duty"] in contract_md


def test_business_domain_override_mechanism():
    reset_role_overrides()
    try:
        register_role_override("reviewer", {
            "duty": "Custom domain business review guard",
            "boundaries": ["Strict custom domain check", "Do not bypass"],
        })
        roles = get_dev_roles()
        assert roles["reviewer"]["duty"] == "Custom domain business review guard"
        assert "Strict custom domain check" in roles["reviewer"]["boundaries"]
        # Architect should remain pristine
        assert roles["architect"]["duty"] == DEV_ROLES["architect"]["duty"]
    finally:
        reset_role_overrides()

    # Verify reset
    pristine_roles = get_dev_roles()
    assert pristine_roles["reviewer"]["duty"] == DEV_ROLES["reviewer"]["duty"]


def test_override_unknown_role_rejected():
    with pytest.raises(ValueError, match="cannot_override_unknown_role"):
        register_role_override("non_existent_role", {"duty": "invalid"})

