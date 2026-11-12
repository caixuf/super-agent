import os
from pathlib import Path
import pytest

from agentcore.colleague_containment import (
    AttemptBoundSandboxAdapter,
    SANDBOX_EXEMPT_ROLES,
    is_role_sandbox_exempt,
)
from install.worker.dev_exec_runner import _wrap_with_bwrap_sandbox


def test_sandbox_exempt_roles_canonical():
    """Verify default privileged roles for host operations."""
    assert "maintainer" in SANDBOX_EXEMPT_ROLES
    assert "ops" in SANDBOX_EXEMPT_ROLES
    assert is_role_sandbox_exempt("maintainer") is True
    assert is_role_sandbox_exempt("ops") is True
    assert is_role_sandbox_exempt("MAINTAINER") is True
    assert is_role_sandbox_exempt("cases") is False
    assert is_role_sandbox_exempt("dev") is False
    assert is_role_sandbox_exempt("triage") is False
    assert is_role_sandbox_exempt("test") is False
    assert is_role_sandbox_exempt("reviewer") is False
    assert is_role_sandbox_exempt(None) is False
    assert is_role_sandbox_exempt("") is False


def test_sandbox_exempt_roles_env_override(monkeypatch: pytest.MonkeyPatch):
    """Verify custom roles can be granted host privilege via environment variable."""
    monkeypatch.setenv("SUPERAGENT_SANDBOX_EXEMPT_ROLES", "atai,custom_worker")
    assert is_role_sandbox_exempt("atai") is True
    assert is_role_sandbox_exempt("custom_worker") is True
    assert is_role_sandbox_exempt("maintainer") is True
    assert is_role_sandbox_exempt("cases") is False


def test_wrap_with_bwrap_sandbox_privileged_role(monkeypatch: pytest.MonkeyPatch):
    """Verify maintainer and ops bypass bwrap and return host execution metadata."""
    monkeypatch.setenv("SUPERAGENT_ENABLE_BWRAP", "1")
    cmd = ["python3", "-m", "worker"]
    
    wrapped_cmd, meta = _wrap_with_bwrap_sandbox(cmd, route={"role": "maintainer"})
    assert wrapped_cmd == cmd
    assert meta is not None
    assert meta["backend"] == "host"
    assert meta["role"] == "maintainer"
    assert meta["exempt"] is True
    assert meta["reason"] == "privileged_role_exemption"
    assert meta["binding"]["ok"] is True


def test_wrap_with_bwrap_sandbox_standard_role_uses_bwrap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Verify non-exempt roles like cases continue to use bwrap sandbox when active."""
    if not AttemptBoundSandboxAdapter.is_bwrap_functional():
        pytest.skip("bwrap not functional in this environment")

    wdir = tmp_path / "work"
    wdir.mkdir()
    att_dir = tmp_path / "attempt"
    att_dir.mkdir()

    monkeypatch.setenv("SUPERAGENT_ENABLE_BWRAP", "1")
    monkeypatch.setenv("DEV_WORKDIR", str(wdir))
    monkeypatch.setenv("SUPERAGENT_ATTEMPT_DIR", str(att_dir))
    monkeypatch.setenv("DEV_ROLE", "cases")

    cmd = ["/usr/bin/echo", "hello"]
    wrapped_cmd, meta = _wrap_with_bwrap_sandbox(cmd, route={"role": "cases"})
    assert wrapped_cmd != cmd
    assert wrapped_cmd[0].endswith("bwrap")
    assert meta is not None
    assert meta["backend"] == "bwrap"

