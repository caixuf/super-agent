"""Tests for Profile Overlay packaging and application."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tarfile

import pytest

from agentcore.profile_overlay import (
    apply_profile_overlay,
    collect_profile_overlay,
    merge_mcp_into_profile_config,
    resolve_profile_dir,
)

REPO = Path(__file__).resolve().parents[1]
_pkg_tool_path = REPO / "tools" / "package_superagent.py"
_pkg_spec = importlib.util.spec_from_file_location("package_superagent", _pkg_tool_path)
package_superagent = importlib.util.module_from_spec(_pkg_spec)
_pkg_spec.loader.exec_module(package_superagent)
build_package = package_superagent.build_package


def test_collect_and_apply_profile_overlay(tmp_path: Path):
    src_profile = tmp_path / "src_profile"
    src_profile.mkdir()

    (src_profile / "SOUL.md").write_text("# Domain Expert Soul\nStrict rules.\n", encoding="utf-8")
    (src_profile / "AGENTS.md").write_text("# Agent hierarchy\n- child_worker\n", encoding="utf-8")

    mem_dir = src_profile / "memories"
    mem_dir.mkdir()
    (mem_dir / "MEMORY.md").write_text("Key domain memory.\n", encoding="utf-8")
    (mem_dir / "USER.md").write_text("User preference data.\n", encoding="utf-8")
    (mem_dir / "TEMP.lock").write_text("lock data", encoding="utf-8")

    skills_dir = src_profile / "skills" / "custom_checker"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text("# Custom Checker Skill\n", encoding="utf-8")
    (skills_dir / "run.py").write_text("print('running checker')\n", encoding="utf-8")
    (skills_dir / ".usage.json").write_text("{}", encoding="utf-8")

    env_file = src_profile / "custom_mcp.env"
    env_file.write_text("MCP_SERVER_URL=http://127.0.0.1:8080\nTOKEN=token_val\n", encoding="utf-8")

    cfg_file = src_profile / "config.yaml"
    cfg_file.write_text(
        "model: test-model\n"
        "mcp_servers:\n"
        "  analyzer:\n"
        "    command: python3\n"
        "    args: ['-m', 'analyzer']\n"
        f"    env_file: '{env_file}'\n",
        encoding="utf-8",
    )

    files, meta = collect_profile_overlay(src_profile, "expert_profile")

    assert "overlay/SOUL.md" in files
    assert "overlay/AGENTS.md" in files
    assert "overlay/memories/MEMORY.md" in files
    assert "overlay/memories/USER.md" in files
    assert "overlay/memories/TEMP.lock" not in files
    assert "overlay/skills/custom_checker/SKILL.md" in files
    assert "overlay/skills/custom_checker/run.py" in files
    assert "overlay/skills/custom_checker/.usage.json" not in files
    assert "overlay/envs/custom_mcp.env" in files
    assert "overlay/metadata.json" in files
    assert "overlay/config_snippet.json" in files

    assert meta["overlay_name"] == "expert_profile"
    assert meta["components"]["has_soul"] is True
    assert "custom_checker" in meta["components"]["skills"]
    assert "analyzer" in meta["components"]["mcp_servers"]

    # Test applying to new profile
    target_profile = tmp_path / "target_profile"
    target_profile.mkdir()
    (target_profile / "config.yaml").write_text("model: production-model\ntimeout: 60\n", encoding="utf-8")

    # Unpack simulated overlay directory
    overlay_dir = tmp_path / "staged_overlay"
    for rel_path, (content, _) in files.items():
        sub = rel_path.removeprefix("overlay/")
        target_f = overlay_dir / sub
        target_f.parent.mkdir(parents=True, exist_ok=True)
        target_f.write_bytes(content)

    result = apply_profile_overlay(target_profile, overlay_dir)
    assert result["status"] == "applied"
    assert result["soul_updated"] is True
    assert result["agents_updated"] is True
    assert "custom_checker" in result["skills_applied"]
    assert "MEMORY.md" in result["memories_applied"]
    assert "analyzer" in result["mcp_servers_applied"]

    # Verify target directory files
    assert (target_profile / "SOUL.md").read_text(encoding="utf-8") == "# Domain Expert Soul\nStrict rules.\n"
    assert (target_profile / "AGENTS.md").read_text(encoding="utf-8") == "# Agent hierarchy\n- child_worker\n"
    assert (target_profile / "skills" / "custom_checker" / "SKILL.md").is_file()
    assert (target_profile / "memories" / "MEMORY.md").is_file()
    assert (target_profile / "custom_mcp.env").is_file()

    # Verify config.yaml was safely merged
    cfg_text = (target_profile / "config.yaml").read_text(encoding="utf-8")
    assert "model: production-model" in cfg_text
    assert "mcp_servers:" in cfg_text
    assert "analyzer:" in cfg_text
    assert str(target_profile / "custom_mcp.env") in cfg_text


def test_merge_mcp_into_profile_config_idempotent(tmp_path: Path):
    prof = tmp_path / "p"
    prof.mkdir()
    cfg = prof / "config.yaml"
    cfg.write_text("model: base\n", encoding="utf-8")

    mcp = {
        "srv1": {
            "command": "python3",
            "env_file": "{{PROFILE_HOME}}/srv1.env",
        }
    }
    count = merge_mcp_into_profile_config(prof, mcp)
    assert count == 1
    content1 = cfg.read_text(encoding="utf-8")
    assert "srv1:" in content1
    assert str(prof / "srv1.env") in content1

    # Second run should be idempotent
    count2 = merge_mcp_into_profile_config(prof, mcp)
    assert count2 == 1
    content2 = cfg.read_text(encoding="utf-8")
    assert content2 == content1
