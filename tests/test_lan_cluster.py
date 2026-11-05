"""Unit tests for the distributed LAN node registry, health probing, and resumable RPC."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
import pytest

from agentcore.lan_cluster import (
    invoke_node_rpc,
    list_nodes,
    normalize_nodes,
    probe_node,
)


SAMPLE_NODES = {
    "prod-runner": {
        "display_name": "Production Runner Node",
        "role": "runner",
        "description": "Primary LAN runner and remote agent endpoint.",
        "capabilities": ["agent_rpc", "http_health", "ssh_probe"],
        "ssh_target": "runner-alias",
        "http_health_url": "http://127.0.0.1:9900/api/health",
        "agent_rpc_cmd": ["/usr/bin/python3", "/opt/agent/remote_rpc.py"],
        "remote_model": "worker-pro",
    },
    "local-sim": {
        "display_name": "Local Simulation Bench",
        "role": "bench",
        "description": "Local GPU simulation node.",
        "capabilities": ["ssh_probe"],
        "ssh_target": "localhost",
    },
}


def test_list_nodes_filters_and_redacts_internal_commands():
    res = list_nodes(SAMPLE_NODES)
    assert res["ok"] is True
    assert res["count"] == 2
    runner = next(n for n in res["nodes"] if n["node_id"] == "prod-runner")
    assert runner["has_agent_rpc"] is True
    assert runner["has_http_health"] is True
    assert "agent_rpc_cmd" not in runner

    filtered = list_nodes(SAMPLE_NODES, capability="agent_rpc")
    assert filtered["count"] == 1
    assert filtered["nodes"][0]["node_id"] == "prod-runner"


def test_probe_node_aggregates_ssh_and_http_checks():
    res = probe_node(
        SAMPLE_NODES,
        "prod-runner",
        http_prober=lambda url, t: {"ok": True, "status_code": 200, "latency_ms": 1.2},
        ssh_prober=lambda host, t: {"ok": True, "latency_ms": 3.4, "remote_hostname": "runner-host"},
    )
    assert res["ok"] is True
    assert res["checks"]["http_health"]["latency_ms"] == 1.2
    assert res["checks"]["ssh"]["remote_hostname"] == "runner-host"


def test_invoke_node_rpc_persists_session_affinity_and_supports_reset(tmp_path: Path):
    captured_payloads = []

    def fake_runner(cmd, *, input, capture_output, text, timeout):
        payload = json.loads(input)
        captured_payloads.append(payload)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps({"ok": True, "run_id": "run-123", "status": "completed"}),
            stderr="",
        )

    r1 = invoke_node_rpc(
        SAMPLE_NODES,
        tmp_path,
        node_id="prod-runner",
        session_key="channel:group-1",
        request_id="req-001",
        operation="submit",
        prompt="Check build queue",
        runner=fake_runner,
    )
    assert r1["ok"] is True
    sid1 = r1["session_id"]
    assert captured_payloads[-1]["protocol_version"] == 2
    assert captured_payloads[-1]["session_id"] == sid1

    r2 = invoke_node_rpc(
        SAMPLE_NODES,
        tmp_path,
        node_id="prod-runner",
        session_key="channel:group-1",
        request_id="req-002",
        operation="submit",
        prompt="Follow up",
        runner=fake_runner,
    )
    assert r2["session_id"] == sid1

    reset_res = invoke_node_rpc(
        SAMPLE_NODES,
        tmp_path,
        node_id="prod-runner",
        session_key="channel:group-1",
        request_id="req-003",
        operation="reset",
        runner=fake_runner,
    )
    assert reset_res["reset"] is True

    r3 = invoke_node_rpc(
        SAMPLE_NODES,
        tmp_path,
        node_id="prod-runner",
        session_key="channel:group-1",
        request_id="req-004",
        operation="submit",
        prompt="Fresh session",
        runner=fake_runner,
    )
    assert r3["session_id"] != sid1


def test_normalize_nodes_rejects_invalid_specs():
    with pytest.raises(ValueError, match="lan_nodes_required"):
        normalize_nodes({})
    with pytest.raises(ValueError, match="invalid_node_id"):
        normalize_nodes({"BAD_ID": {"display_name": "Bad"}})

