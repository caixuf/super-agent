"""Unit tests for scripts/ops/setup_credentials.py zero-secret credentials wizard."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import pytest

from scripts.ops.setup_credentials import (
    mask_secret,
    validate_feishu_credentials,
    validate_model_endpoint,
    update_env_file,
    run_wizard,
)


def test_mask_secret():
    assert mask_secret("") == "<empty>"
    assert mask_secret("short") == "***"
    assert mask_secret("cli_abcdef123456") == "cli_...****"


def test_validate_feishu_credentials():
    ok, msg = validate_feishu_credentials("cli_a1b2c3d4e5f6g7h8", "secret_1234567890abcdef")
    assert ok is True

    ok, msg = validate_feishu_credentials("", "secret_1234567890abcdef")
    assert ok is False
    assert "empty" in msg

    ok, msg = validate_feishu_credentials("cli_123", "short")
    assert ok is False
    assert "too short" in msg


def test_validate_model_endpoint():
    ok, msg = validate_model_endpoint("http://127.0.0.1:4000/v1", "sk-valid-key")
    assert ok is True

    ok, msg = validate_model_endpoint("not_a_url", "sk-valid-key")
    assert ok is False

    ok, msg = validate_model_endpoint("http://127.0.0.1:4000/v1", "")
    assert ok is False


def test_update_env_file_security_and_permissions(tmp_path):
    target = tmp_path / "subdir" / ".env"
    updates = {
        "FEISHU_APP_ID": "cli_test_123456",
        "FEISHU_APP_SECRET": "secret_abcdef123456",
    }

    # 1. Fresh file creation
    res = update_env_file(target, updates)
    assert res["ok"] is True
    assert target.exists()
    mode = target.stat().st_mode
    assert stat.S_IMODE(mode) == 0o600

    content = target.read_text(encoding="utf-8")
    assert "FEISHU_APP_ID=cli_test_123456" in content
    assert "FEISHU_APP_SECRET=secret_abcdef123456" in content

    # 2. Update existing key and preserve comments
    target.write_text("# Existing comment\nEXISTING_VAR=foo\nFEISHU_APP_ID=old\n", encoding="utf-8")
    update_env_file(target, {"FEISHU_APP_ID": "cli_updated_999", "NEW_KEY": "val"})

    updated_content = target.read_text(encoding="utf-8")
    assert "# Existing comment" in updated_content
    assert "EXISTING_VAR=foo" in updated_content
    assert "FEISHU_APP_ID=cli_updated_999" in updated_content
    assert "NEW_KEY=val" in updated_content

    # 3. Refuse symlink
    link = tmp_path / "symlink_env"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        update_env_file(link, {"FOO": "bar"})


def test_run_wizard_cli_mode(tmp_path):
    target = tmp_path / "test.env"
    code = run_wizard(
        target_file=str(target),
        app_id="cli_wizard_app123",
        app_secret="wizard_secret_12345678",
        model_base_url="http://127.0.0.1:4000/v1",
        model_api_key="sk-model-key-xyz",
        dry_run=False,
        interactive=False,
    )
    assert code == 0
    assert target.exists()
    content = target.read_text(encoding="utf-8")
    assert "cli_wizard_app123" in content
    assert "sk-model-key-xyz" in content


def test_setup_credentials_main_cli(tmp_path):
    from scripts.ops.setup_credentials import main

    target = tmp_path / "main_test.env"
    argv = [
        "--non-interactive",
        "--target-file", str(target),
        "--app-id", "cli_main_test_app",
        "--app-secret", "main_secret_long_enough_1234",
    ]
    exit_code = main(argv)
    assert exit_code == 0
    assert target.exists()
    assert "FEISHU_APP_ID=cli_main_test_app" in target.read_text(encoding="utf-8")

    # Empty inputs with non-interactive returns 0
    assert main(["--non-interactive"]) == 0
