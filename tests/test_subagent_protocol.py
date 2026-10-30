"""Unit tests for agentcore.subagent_protocol."""
from __future__ import annotations

import pytest

from agentcore.subagent_protocol import (
    MAX_COMPLETION_CRITERIA,
    MAX_SUBAGENT_UNITS,
    SUBAGENT_RESULT_SCHEMA,
    SubagentUnit,
    assemble_subagent_prompt,
    contain_cascading_failure,
    detect_boundary_conflicts,
    validate_subagent_result,
)


def test_subagent_unit_creation():
    unit = SubagentUnit(
        unit_id="unit-parser",
        title="Parser Refactor",
        goal="Extract parser module into separate file",
        target_paths=("src/parser.py", "tests/test_parser.py"),
        do_not_touch=("src/core.py",),
        criteria=("Unit tests pass", "Zero linter warnings"),
        role_name="refactor_specialist",
    )
    assert unit.unit_id == "unit-parser"
    assert len(unit.target_paths) == 2
    assert len(unit.criteria) == 2


def test_subagent_unit_validation_errors():
    with pytest.raises(ValueError, match="invalid unit_id"):
        SubagentUnit(unit_id="bad id with spaces", title="T", goal="G")

    with pytest.raises(ValueError, match="cannot be empty"):
        SubagentUnit(unit_id="u1", title="T", goal="   ")

    too_many_criteria = tuple(f"crit_{i}" for i in range(MAX_COMPLETION_CRITERIA + 1))
    with pytest.raises(ValueError, match="criteria count"):
        SubagentUnit(unit_id="u2", title="T", goal="Valid goal", criteria=too_many_criteria)


def test_detect_boundary_conflicts_write_overlap():
    u1 = SubagentUnit(
        unit_id="worker-a",
        title="Feature A",
        goal="Implement feature A",
        target_paths=("shared/config.py", "module_a/service.py"),
    )
    u2 = SubagentUnit(
        unit_id="worker-b",
        title="Feature B",
        goal="Implement feature B",
        target_paths=("shared/config.py", "module_b/service.py"),
    )

    conflicts = detect_boundary_conflicts([u1, u2])
    assert len(conflicts) == 1
    assert conflicts[0]["conflict_type"] == "write_overlap"
    assert "shared/config.py" in conflicts[0]["paths"]
    assert conflicts[0]["units"] == ["worker-a", "worker-b"]


def test_detect_boundary_conflicts_self_contradiction_and_protected():
    u1 = SubagentUnit(
        unit_id="worker-a",
        title="Refactor",
        goal="Refactor DB module",
        target_paths=("core/database.py",),
        do_not_touch=("core/database.py", "core/models.py"),
    )
    conflicts = detect_boundary_conflicts([u1])
    assert len(conflicts) == 1
    assert conflicts[0]["conflict_type"] == "self_contradiction"
    assert "core/database.py" in conflicts[0]["paths"]

    u2 = SubagentUnit(
        unit_id="worker-b",
        title="Config",
        goal="Update config",
        target_paths=("secure/credentials.json",),
    )
    conflicts_prot = detect_boundary_conflicts([u2], protected_paths=("secure/credentials.json",))
    assert len(conflicts_prot) == 1
    assert conflicts_prot[0]["conflict_type"] == "protected_path_violation"


def test_detect_boundary_conflicts_disjoint_paths():
    u1 = SubagentUnit(
        unit_id="worker-a",
        title="Module A",
        goal="Work on A",
        target_paths=("a/file.py",),
        do_not_touch=("b/file.py",),
    )
    u2 = SubagentUnit(
        unit_id="worker-b",
        title="Module B",
        goal="Work on B",
        target_paths=("b/file.py",),
        do_not_touch=("a/file.py",),
    )

    conflicts = detect_boundary_conflicts([u1, u2])
    assert conflicts == []


def test_detect_boundary_conflicts_directory_hierarchy_and_glob():
    # Directory tree containment: src vs src/core/main.py
    u1 = SubagentUnit(unit_id="w-dir", title="Dir", goal="G", target_paths=("src/",))
    u2 = SubagentUnit(unit_id="w-file", title="File", goal="G", target_paths=("src/core/main.py",))
    conflicts = detect_boundary_conflicts([u1, u2])
    assert len(conflicts) == 1
    assert conflicts[0]["conflict_type"] == "write_overlap"
    assert "src" in conflicts[0]["paths"]

    # Protected directory containment: secrets vs secrets/token.json
    u3 = SubagentUnit(unit_id="w-secret", title="Secret", goal="G", target_paths=("secrets/token.json",))
    conf_prot = detect_boundary_conflicts([u3], protected_paths=("secrets",))
    assert len(conf_prot) == 1
    assert conf_prot[0]["conflict_type"] == "protected_path_violation"

    # Glob matching in do_not_touch: *.env vs local.env
    u4 = SubagentUnit(
        unit_id="w-glob",
        title="Glob",
        goal="G",
        target_paths=("local.env",),
        do_not_touch=("*.env",),
    )
    conf_glob = detect_boundary_conflicts([u4])
    assert len(conf_glob) == 1
    assert conf_glob[0]["conflict_type"] == "self_contradiction"


def test_assemble_subagent_prompt():
    unit = SubagentUnit(
        unit_id="sub-task-1",
        title="Add validation logic",
        goal="Validate user email format",
        target_paths=("auth/validator.py",),
        do_not_touch=("auth/db.py",),
        criteria=("Regex checks email correctly", "Tests pass"),
        role_name="security_reviewer",
        role_contract="Focus on input sanitation.",
    )

    prompt = assemble_subagent_prompt("Build authentication microservice", unit)

    # Verify key protocol sections
    assert "# Global Goal" in prompt
    assert "Instruction Fidelity Protocol 1: Goal Echo-Back" in prompt
    assert "Instruction Fidelity Protocol 2: Verification & Stop Discipline" in prompt
    assert "Instruction Fidelity Protocol 3: Capped Structural Search" in prompt
    assert "Subagent Assignment: sub-task-1" in prompt
    assert "**Allowed Target Paths**: auth/validator.py" in prompt
    assert "**DO NOT TOUCH (Forbidden)**: auth/db.py" in prompt
    assert "1. Regex checks email correctly" in prompt
    assert "Role Instructions (security_reviewer)" in prompt
    assert "Focus on input sanitation." in prompt
    assert SUBAGENT_RESULT_SCHEMA in prompt

    # Verify omitting shared preamble
    prompt_no_head = assemble_subagent_prompt("Build authentication microservice", unit, shared_preamble=False)
    assert "Instruction Fidelity Protocol 1" not in prompt_no_head
    assert "Subagent Assignment: sub-task-1" in prompt_no_head


def test_validate_subagent_result_valid_json_string():
    raw_markdown = """
Here is my final report after completing the work:
```json
{
  "schema_version": "subagent_result/v1",
  "unit_id": "sub-task-1",
  "status": "completed",
  "summary": "Implemented email validation logic and verified with pytest.",
  "deliverables": ["auth/validator.py", "tests/test_validator.py"],
  "criteria_met": [true, true],
  "verification_evidence": "All 4 unit tests passed in 0.12s"
}
```
Done!
"""
    ok, data, errors = validate_subagent_result(raw_markdown)
    assert ok is True
    assert errors == []
    assert data["unit_id"] == "sub-task-1"
    assert data["status"] == "completed"
    assert len(data["deliverables"]) == 2


def test_validate_subagent_result_unmet_criteria():
    data = {
        "schema_version": "subagent_result/v1",
        "unit_id": "sub-task-1",
        "status": "completed",
        "summary": "Finished work but one test failed",
        "deliverables": ["auth/validator.py"],
        "criteria_met": [True, False],  # Second criterion false!
    }
    ok, parsed, errors = validate_subagent_result(data)
    assert ok is False
    assert any("all criteria_met entries must be true" in e for e in errors)


def test_validate_subagent_result_rejects_non_boolean_criteria():
    # String "false" or integer 1 must not bypass boolean validation
    data1 = {
        "schema_version": "subagent_result/v1",
        "unit_id": "sub-task-1",
        "status": "completed",
        "summary": "Finished",
        "criteria_met": ["false"],
    }
    ok1, _, errs1 = validate_subagent_result(data1)
    assert ok1 is False
    assert any("all criteria_met entries must be true" in e for e in errs1)

    data2 = {
        "schema_version": "subagent_result/v1",
        "unit_id": "sub-task-1",
        "status": "completed",
        "summary": "Finished",
        "criteria_met": [1],
    }
    ok2, _, errs2 = validate_subagent_result(data2)
    assert ok2 is False
    assert any("all criteria_met entries must be true" in e for e in errs2)


def test_validate_subagent_result_with_earlier_json_and_fences():
    raw_markdown = """
Here is an example schema snippet I used during execution:
```json
{"sample_code": 123, "not_the_result": true}
```

And here is my final completion envelope:
```json
{
  "schema_version": "subagent_result/v1",
  "unit_id": "worker-finish",
  "status": "completed",
  "summary": "Successfully extracted module and verified with pytest.",
  "deliverables": ["src/service.py"],
  "criteria_met": [true],
  "verification_evidence": "Passes 10 unit tests."
}
```
Done!
"""
    ok, data, errors = validate_subagent_result(raw_markdown)
    assert ok is True
    assert errors == []
    assert data["unit_id"] == "worker-finish"
    assert data["status"] == "completed"


def test_validate_subagent_result_missing_fields():
    ok, parsed, errors = validate_subagent_result("not a json string at all")
    assert ok is False
    assert "no JSON object found in output" in errors

    ok2, parsed2, errors2 = validate_subagent_result({"schema_version": "subagent_result/v1"})
    assert ok2 is False
    assert "unit_id is required" in errors2
    assert "summary is required" in errors2


def test_contain_cascading_failure():
    all_units = ["unit-1", "unit-2", "unit-3"]

    # Permanent failure halts all siblings
    perm_dec = {"action": "stop", "kind": "permanent"}
    res_perm = contain_cascading_failure("unit-1", perm_dec, all_units)
    assert res_perm["containment_action"] == "halt_all"
    assert res_perm["fail_closed"] is True
    assert set(res_perm["halted_siblings"]) == {"unit-2", "unit-3"}

    # Persistent failure pauses for batch replan
    pers_dec = {"action": "replan", "kind": "persistent"}
    res_pers = contain_cascading_failure("unit-2", pers_dec, all_units)
    assert res_pers["containment_action"] == "pause_for_replan"
    assert res_pers["fail_closed"] is True
    assert set(res_pers["halted_siblings"]) == {"unit-1", "unit-3"}

    # Transient failure allows retry without halting siblings
    trans_dec = {"action": "retry", "kind": "transient"}
    res_trans = contain_cascading_failure("unit-1", trans_dec, all_units)
    assert res_trans["containment_action"] == "isolate_unit_retry"
    assert res_trans["fail_closed"] is False
    assert res_trans["halted_siblings"] == []

    # Escalation halts execution for operator review
    esc_dec = {"action": "escalate", "kind": "external_wait"}
    res_esc = contain_cascading_failure("unit-3", esc_dec, all_units)
    assert res_esc["containment_action"] == "escalate_batch"
    assert res_esc["fail_closed"] is True
