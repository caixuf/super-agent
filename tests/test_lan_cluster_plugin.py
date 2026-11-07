"""lan_cluster plugin: a thin adapter over the engine's cluster semantics.

The engine owns the meaning of "no cluster configured": an absent or empty node
map is an empty cluster, not a broken one (``agentcore.lan_cluster``). The
plugin must therefore not turn "no cluster configured" into a registration
failure — otherwise the ``lan_node_*`` tools never reach the tool surface even
though the capability is switched on by the official default install.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "lan_cluster_plugin", ROOT / "plugins" / "lan_cluster" / "__init__.py")
assert _spec and _spec.loader
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)

NODE_MAP = {
    "prod-runner": {
        "display_name": "Production Runner Node",
        "role": "runner",
        "description": "Primary LAN runner and remote agent endpoint.",
        "capabilities": ["agent_rpc"],
    },
}


class Ctx:
    def __init__(self, settings):
        self.settings = settings
        self.tools = []
        self.sections = []

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_system_prompt_section(self, *args, **kwargs):
        self.sections.append((args, kwargs))


def _ctx(**settings):
    base = {"enabled": True, "engine_root": str(ROOT),
            "state_dir": str(ROOT / "agentcore-state" / "lan-cluster")}
    base.update(settings)
    return Ctx(base)


def _handler(ctx, name):
    plugin.register(ctx)
    return next(tool["handler"] for tool in ctx.tools if tool["name"] == name)


def test_unconfigured_cluster_still_registers_the_tools():
    ctx = _ctx()
    plugin.register(ctx)
    assert sorted(tool["name"] for tool in ctx.tools) == [
        "lan_node_list", "lan_node_probe", "lan_node_rpc"]
    assert all(tool["toolset"] == "lan_cluster" for tool in ctx.tools)


def test_empty_cluster_lists_nothing_instead_of_failing():
    ctx = _ctx()
    out = json.loads(_handler(ctx, "lan_node_list")({}, session_id="s"))
    assert out == {"ok": True, "count": 0, "nodes": []}


def test_probe_and_rpc_report_that_no_cluster_is_configured():
    ctx = _ctx()
    probe = json.loads(_handler(ctx, "lan_node_probe")(
        {"node_id": "prod-runner"}, session_id="s"))
    rpc = json.loads(_handler(ctx, "lan_node_rpc")(
        {"node_id": "prod-runner", "request_id": "r1"}, session_id="s"))
    assert probe == {"ok": False, "error": "lan_nodes_required"}
    assert rpc == {"ok": False, "error": "lan_nodes_required"}


def test_configured_cluster_is_validated_and_listed():
    ctx = _ctx(nodes=NODE_MAP)
    out = json.loads(_handler(ctx, "lan_node_list")({}, session_id="s"))
    assert out["ok"] is True
    assert out["count"] == 1
    assert out["nodes"][0]["node_id"] == "prod-runner"


def test_malformed_cluster_still_refuses_to_register():
    ctx = _ctx(nodes={"BAD_ID": {"display_name": "Bad"}})
    with pytest.raises(ValueError, match="invalid_node_id"):
        plugin.register(ctx)


def test_explicitly_empty_node_map_is_still_an_empty_cluster():
    ctx = _ctx(nodes={})
    plugin.register(ctx)
    assert sorted(tool["name"] for tool in ctx.tools) == [
        "lan_node_list", "lan_node_probe", "lan_node_rpc"]
    out = json.loads(_handler(ctx, "lan_node_list")({}, session_id="s"))
    assert out == {"ok": True, "count": 0, "nodes": []}


def test_a_node_map_that_is_not_a_mapping_refuses_to_register():
    ctx = _ctx(nodes=["not-a-mapping"])
    with pytest.raises(ValueError, match="lan_nodes_must_be_mapping"):
        plugin.register(ctx)
