"""Prefer B thin — portable colleague pack (parse / redact / import / export)."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from agentcore.colleague_catalog import (
    FIELDS,
    MAX_ROLES,
    NOTICE,
    default_dev_catalog,
    normalize_catalog,
)
from agentcore.colleague_dev import DEV_ROLES, get_dev_roles
from agentcore.colleague_pack import (
    PACK_FORMAT,
    PACK_NOTICE,
    PackError,
    dump_pack_json,
    export_redacted,
    import_additive,
    load_pack_file,
    parse_pack,
    set_yaml_loader,
    redact_pack,
    validate_pack,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "colleague_pack"


def _entry(name="Analyst", duty="Review supplied evidence and report findings."):
    return {
        "display_name": name,
        "duty": duty,
        "can_do": ["Summarize source-backed findings"],
        "boundaries": ["Read only", "Do not change files"],
    }


def test_b1_parse_and_validate_happy_json():
    pack = validate_pack(FIXTURES / "happy.json")
    assert pack["pack_format"] == PACK_FORMAT
    assert pack["notice"] == PACK_NOTICE == NOTICE
    assert [r["role_id"] for r in pack["roles"]] == ["analyst", "scribe"]
    for role in pack["roles"]:
        assert set(role) >= FIELDS | {"role_id", "skills", "routines", "playbook"}
        assert all(s["enabled"] is False for s in role["skills"])
        assert all(r["paused"] is True for r in role["routines"])


def test_b1_rejects_bad_role_id_and_missing_fields():
    with pytest.raises(PackError, match="invalid_role_id"):
        validate_pack({
            "pack_format": PACK_FORMAT,
            "roles": [{"role_id": "../escape", **_entry()}],
        })
    with pytest.raises(PackError, match="missing_fields"):
        validate_pack({
            "pack_format": PACK_FORMAT,
            "roles": [{"role_id": "analyst", "display_name": "A", "duty": "d",
                       "can_do": ["x"]}],
        })
    with pytest.raises(PackError):
        validate_pack({
            "pack_format": PACK_FORMAT,
            "roles": [{"role_id": "analyst", **_entry(), "queue_root": "/tmp"}],
        })


def test_b1_yaml_frontmatter_md_optional():
    yaml = pytest.importorskip("yaml")
    set_yaml_loader(yaml.safe_load)
    try:
        pack = validate_pack(FIXTURES / "playbook.md")
        assert pack["roles"][0]["role_id"] == "scout"
        assert pack["playbook"] and "grants no permissions" in pack["playbook"].lower()
        assert pack["roles"][0]["skills"][0] == {"id": "open_source_scan", "enabled": False}
        assert pack["roles"][0]["routines"][0] == {"id": "morning_sweep", "paused": True}
    finally:
        set_yaml_loader(None)


def test_b2_secret_keys_and_values_rejected():
    with pytest.raises(PackError, match="banned_key"):
        parse_pack(FIXTURES / "with_secret_key.json")
    with pytest.raises(PackError, match="secret_shaped_value"):
        parse_pack(FIXTURES / "with_secret_value.json")
    with pytest.raises(PackError, match="absolute_host_path_banned"):
        parse_pack({
            "pack_format": PACK_FORMAT,
            "roles": [{
                "role_id": "analyst",
                **_entry(),
                "skills": ["/home/secret/skill.md"],
            }],
        })


def test_b2_redact_strips_banned_before_export_shape():
    dirty = {
        "pack_format": PACK_FORMAT,
        "api_token": "should-go",
        "roles": [{
            "role_id": "analyst",
            **_entry(duty="password=hunter2secretvalue"),
            "cookie": "session=abc",
        }],
    }
    cleaned = redact_pack(dirty)
    blob = json.dumps(cleaned)
    assert "api_token" not in cleaned
    assert "cookie" not in cleaned["roles"][0]
    assert "hunter2" not in blob
    assert "[REDACTED]" in cleaned["roles"][0]["duty"]


def test_b3_b4_skills_off_routines_paused_after_import():
    base = {"analyst": _entry()}
    result = import_additive(base, FIXTURES / "happy.json")
    assert result["ok"] is True
    assert all(s["enabled"] is False for s in result["skills"])
    assert all(r["paused"] is True for r in result["routines"])
    assert result["routines_seam"] == "metadata_only"
    assert "no scheduler" in result["deliver_or_reason"]


def test_b5_additive_under_max_roles_preserves_existing():
    base = {"analyst": _entry()}
    original = deepcopy(base)
    pack = {
        "pack_format": PACK_FORMAT,
        "roles": [{"role_id": "scribe", **_entry("Scribe", "Capture decisions.")}],
    }
    result = import_additive(base, pack)
    assert result["roles"]["analyst"] == original["analyst"]
    assert "scribe" in result["roles"]
    assert result["added"] == ["scribe"]
    assert result["unchanged"] == []
    # DEV_ROLES floor untouched.
    assert get_dev_roles() == DEV_ROLES
    assert default_dev_catalog()["colleagues"]


def test_b5_rejects_when_max_roles_exceeded():
    base = {f"role_{i}": _entry(f"R{i}") for i in range(MAX_ROLES)}
    pack = {
        "pack_format": PACK_FORMAT,
        "roles": [{"role_id": "extra_role", **_entry("Extra")}],
    }
    with pytest.raises(PackError, match="max_roles_exceeded"):
        import_additive(base, pack)


def test_b6_conflict_reject_identical_idempotent():
    base = {"analyst": _entry()}
    identical = validate_pack({
        "pack_format": PACK_FORMAT,
        "roles": [{"role_id": "analyst", **_entry()}],
    })
    again = import_additive(base, identical)
    assert again["added"] == []
    assert again["unchanged"] == ["analyst"]
    assert again["roles"]["analyst"] == base["analyst"]

    with pytest.raises(PackError, match="role_conflict:analyst"):
        import_additive(base, FIXTURES / "conflict_duty.json")


def test_b7_export_redacted_defaults_and_round_trip():
    roles = {"analyst": _entry(), "scribe": _entry("Scribe", "Capture decisions.")}
    exported = export_redacted(
        roles,
        skills={"analyst": ["evidence_summary"], "scribe": ["note_template"]},
        routines={"analyst": ["weekly_digest"]},
        playbook="Descriptive only.",
    )
    blob = dump_pack_json(exported)
    assert "sk-" not in blob
    assert NOTICE.split(".")[0] in exported["notice"]
    for role in exported["roles"]:
        assert all(s["enabled"] is False for s in role["skills"])
        assert all(r["paused"] is True for r in role["routines"])
    with pytest.raises(PackError, match="skill_absolute_path_banned"):
        export_redacted(roles, skills={"analyst": ["/etc/passwd"]})

    imported = import_additive({}, exported)
    assert set(imported["roles"]) == {"analyst", "scribe"}
    assert all(s["enabled"] is False for s in imported["skills"])


def test_b10_notice_grants_no_permissions():
    pack = validate_pack(FIXTURES / "happy.json")
    assert "not instructions or permissions" in pack["notice"]
    exported = export_redacted({"analyst": _entry()})
    assert exported["notice"] == NOTICE


def test_load_pack_file_requires_absolute_host_path(tmp_path):
    target = tmp_path / "pack.json"
    target.write_text((FIXTURES / "happy.json").read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(PackError, match="pack_path_must_be_absolute"):
        load_pack_file("relative/pack.json")
    loaded = load_pack_file(target.resolve())
    assert loaded["roles"][0]["role_id"] == "analyst"


def test_import_never_mutates_caller_base_or_dev_roles():
    base = {"analyst": _entry()}
    snapshot = deepcopy(base)
    dev_before = deepcopy(DEV_ROLES)
    import_additive(base, {
        "pack_format": PACK_FORMAT,
        "roles": [{"role_id": "scribe", **_entry("Scribe", "Capture decisions.")}],
    })
    assert base == snapshot
    assert DEV_ROLES == dev_before


def test_catalog_regression_normalize_still_green():
    # Prefer B must not loosen catalog ontology.
    roles = {"analysis": _entry()}
    payload = normalize_catalog(roles)
    assert payload["ok"] and payload["notice"] == NOTICE
    with pytest.raises(ValueError):
        normalize_catalog({**{f"r{i}": _entry(f"N{i}") for i in range(MAX_ROLES)}, "extra": _entry()})
