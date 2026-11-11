"""Tests for LobeChat deployment setup and Bot presets exporter."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from install.setup_lobechat import (
    render_lobechat_compose,
    render_lobechat_service,
    main as setup_main,
)
from tools.export_lobechat_agents import (
    build_lobechat_agent_config,
    build_lobechat_master_agent,
    export_all_agents,
    main as export_main,
)
from agentcore.colleague_dev import DEV_ROLES


def test_render_lobechat_compose_default():
    yaml_content = render_lobechat_compose(
        port=3210,
        api_url="http://host.docker.internal:8642/v1",
        api_key="sk-testkey",
        access_code="supersecret",
    )
    assert 'image: lobehub/lobe-chat:latest' in yaml_content
    assert '"3210:3210"' in yaml_content
    assert 'OPENAI_API_KEY=sk-testkey' in yaml_content
    assert 'OPENAI_PROXY_URL=http://host.docker.internal:8642/v1' in yaml_content
    assert 'ACCESS_CODE=supersecret' in yaml_content
    assert 'host.docker.internal:host-gateway' in yaml_content


def test_render_lobechat_service():
    service_content = render_lobechat_service(
        compose_file=Path("/home/user/.hermes/lobechat/docker-compose.yml"),
        working_dir=Path("/home/user/.hermes/lobechat"),
    )
    assert "[Unit]" in service_content
    assert "Description=Super Agent LobeChat Web UI Service" in service_content
    assert 'ExecStart=/usr/bin/docker compose -f "/home/user/.hermes/lobechat/docker-compose.yml" up' in service_content
    assert 'WorkingDirectory="/home/user/.hermes/lobechat"' in service_content
    assert "Restart=always" in service_content


def test_export_single_agent_config():
    role_key = "architect"
    role_data = DEV_ROLES[role_key]
    agent = build_lobechat_agent_config(role_key, role_data, default_model="hermes-agent")

    assert agent["schemaVersion"] == "v1"
    assert agent["identifier"] == "superagent-architect"
    assert agent["meta"]["title"] == f"SuperAgent: {role_data['display_name']}"
    assert agent["meta"]["avatar"] == "📐"
    assert "SuperAgent" in agent["meta"]["tags"]
    assert "architect" in agent["meta"]["tags"]
    assert agent["config"]["model"] == "hermes-agent"
    assert len(agent["config"]["systemRole"]) > 20


def test_export_master_agent():
    master = build_lobechat_master_agent(default_model="hermes-agent")
    assert master["identifier"] == "superagent-master"
    assert master["meta"]["title"] == "Super Agent 主智能体"
    assert master["meta"]["avatar"] == "🧠"
    assert "Super Agent" in master["config"]["systemRole"]


def test_export_all_agents_structure():
    pack = export_all_agents(default_model="hermes-agent")
    assert pack["exportType"] == "sessions"
    assert pack["version"] == 3
    sessions = pack["state"]["sessions"]

    # 1 master agent + 8 dev colleague roles = 9 sessions
    assert len(sessions) == 1 + len(DEV_ROLES)
    session_ids = [s["id"] for s in sessions]
    assert "superagent-master" in session_ids
    for r in DEV_ROLES:
        assert f"superagent-{r}" in session_ids


def test_setup_main_plan_and_apply(tmp_path, monkeypatch, capsys):
    out_dir = tmp_path / "lobechat"
    unit_dir = tmp_path / "systemd"

    # 1. Plan mode
    test_args = [
        "setup_lobechat.py",
        "--port", "3210",
        "--out-dir", str(out_dir),
        "--unit-dir", str(unit_dir),
    ]
    monkeypatch.setattr("sys.argv", test_args)
    rc = setup_main()
    assert rc == 0
    assert not (out_dir / "docker-compose.yml").exists()

    # 2. Apply mode
    test_args_apply = [
        "setup_lobechat.py",
        "--port", "3210",
        "--out-dir", str(out_dir),
        "--unit-dir", str(unit_dir),
        "--apply",
        "--with-service",
    ]
    monkeypatch.setattr("sys.argv", test_args_apply)
    rc_apply = setup_main()
    assert rc_apply == 0
    assert (out_dir / "docker-compose.yml").exists()
    assert (out_dir / "start.sh").exists()
    assert (out_dir / "stop.sh").exists()
    assert (unit_dir / "superagent-lobechat.service").exists()


def test_export_main_file(tmp_path, monkeypatch):
    out_file = tmp_path / "superagent_bots.json"
    test_args = [
        "export_lobechat_agents.py",
        "-o", str(out_file),
    ]
    monkeypatch.setattr("sys.argv", test_args)
    rc = export_main()
    assert rc == 0
    assert out_file.exists()

    content = json.loads(out_file.read_text(encoding="utf-8"))
    assert content["exportType"] == "sessions"
    assert len(content["state"]["sessions"]) == 9

