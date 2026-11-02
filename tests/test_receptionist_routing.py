"""Receptionist/worker routing plan is read-only and fail-closed."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import yaml

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "tools/plan_receptionist_routing.py"


def _load():
    spec = importlib.util.spec_from_file_location("routing_plan", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plan_requests_flash_reception_and_deep_worker(tmp_path, monkeypatch):
    module = _load()
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({
        "model": {"default": "main", "provider": "litellm"},
        "display": {"busy_input_mode": "queue"},
        "delegation": {"backends": {"model_map": {
            "default": "fast",
            "options": [
                {"name": "fast", "model": "flash"},
                {"name": "deep", "model": "main"},
                {"name": "architect", "model": "pro"},
            ],
        }}},
    }), encoding="utf-8")
    values = {
        "model.default": "main",
        "delegation.backends.model_map.default": "fast",
        "display.busy_input_mode": "queue",
    }
    monkeypatch.setattr(module, "_value", lambda hermes, profile, key: values[key])
    before = config.read_bytes()
    plan = module.build_plan(tmp_path, "demo", "/usr/bin/hermes")
    assert plan["blockers"] == []
    assert plan["desired"] == {
        "model.default": "flash",
        "delegation.backends.model_map.default": "deep",
        "display.busy_input_mode": "steer",
    }
    assert plan["changes"]["model.default"] == {"from": "main", "to": "flash"}
    assert config.read_bytes() == before


def test_plan_fails_closed_without_required_routes(tmp_path, monkeypatch):
    module = _load()
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({
        "model": {"default": "main", "provider": "litellm"},
        "delegation": {"backends": {"model_map": {"default": "fast", "options": []}}},
    }), encoding="utf-8")
    monkeypatch.setattr(module, "_value", lambda *args: "")
    plan = module.build_plan(tmp_path, "demo", "/usr/bin/hermes")
    assert "missing_delegation_route_fast" in plan["blockers"]
    assert "missing_delegation_route_deep" in plan["blockers"]
    assert "missing_delegation_route_architect" in plan["blockers"]


def test_apply_uses_official_config_set_and_restores_on_failure(tmp_path, monkeypatch):
    module = _load()
    config = tmp_path / "config.yaml"
    original = "# keep\nmodel:\n  default: main\n"
    config.write_text(original, encoding="utf-8")
    plan = {
        "profile": "demo", "profile_home": str(tmp_path), "config": str(config),
        "changes": {"model.default": {"from": "main", "to": "flash"}},
    }
    calls = []
    def fake_run(cmd, *, env=None):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, "", "fail")
    monkeypatch.setattr(module, "_run", fake_run)
    assert module.apply(plan, "/usr/bin/hermes") == 1
    assert calls[-1][-4:-1] == ["config", "set", "model.default"]
    assert config.read_text() == original
    assert list((tmp_path / "backups/config").glob("config.yaml.receptionist-*"))
