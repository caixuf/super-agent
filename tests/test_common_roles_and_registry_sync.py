"""Tests for Common Infrastructure Roles (maintainer, ops, dev) and Dynamic Registry Sync."""
from __future__ import annotations

from pathlib import Path
import sqlite3
import pytest

from agentcore.colleague_dev import (
    COMMON_ROLES,
    DEV_ROLES,
    get_common_roles,
    get_dev_roles,
    get_role_ownership,
    RolePathOwnership,
    HARD,
)
from plugins.durable_dispatch import build_dispatch_services


def test_common_roles_containment():
    common = get_common_roles()
    assert "maintainer" in common
    assert "ops" in common
    assert "dev" in common

    for role_name in ("maintainer", "ops", "dev"):
        meta = common[role_name]
        assert "display_name" in meta
        assert "duty" in meta
        assert "can_do" in meta
        assert "boundaries" in meta

        own = get_role_ownership(role_name)
        assert isinstance(own, RolePathOwnership)
        assert own.enforce_write == HARD
        assert len(own.owns) > 0


def test_durable_dispatch_dynamic_roster_sync(tmp_path: Path):
    reg_db = tmp_path / "registry.sqlite"
    con = sqlite3.connect(reg_db)
    con.executescript("""
        CREATE TABLE colleague_roles (
            profile_id TEXT NOT NULL,
            role_id TEXT NOT NULL,
            label TEXT NOT NULL,
            duty TEXT NOT NULL,
            boundaries TEXT NOT NULL
        );
        CREATE TABLE colleague_members (
            profile_id TEXT NOT NULL,
            member_id TEXT NOT NULL,
            role_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            status TEXT NOT NULL
        );
        INSERT INTO colleague_roles VALUES ('test', 'atai', '帝江台架测试专家(啊台)', '台架测试执行与归档', '[]');
        INSERT INTO colleague_members VALUES ('test', 'atai', 'atai', '啊台', 'active');
    """)
    con.close()

    q_root = tmp_path / "queues" / "analysis"
    q_root.mkdir(parents=True)

    provider = {
        "engine_root": str(Path.cwd()),
        "queue_root": str(q_root),
        "allowed_roots": [str(tmp_path)],
        "roster_registry_path": str(reg_db),
        "colleagues": {
            "dev": {
                "queue_root": str(tmp_path / "queues" / "dev"),
                "allowed_roots": [str(tmp_path)],
                "display_name": "研发执行",
            }
        }
    }

    services = build_dispatch_services(provider, "test")
    assert "analysis" in services
    assert "dev" in services
    # Dynamically synced from registry.sqlite without config.yaml edit!
    assert "atai" in services
    assert services["atai"].display_name == "帝江台架测试专家(啊台)"
    assert "台架测试执行" in services["atai"].duty
