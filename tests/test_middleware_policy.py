"""Tests for official Hermes Middleware policy (C04, C20, C21)."""
from __future__ import annotations

from unittest.mock import MagicMock
import pytest

from agentcore.middleware_policy import (
    SuperAgentToolMiddleware,
    SuperAgentModelMiddleware,
    register_superagent_middleware,
    TOOL_DENIED,
    WRITE_DENIED,
)


def test_tool_middleware_frontdesk_gate_c04_c21():
    # 1. Frontdesk disabled -> all tools allowed
    mw_disabled = SuperAgentToolMiddleware(frontdesk_enabled=False)
    next_call = MagicMock(return_value={"ok": True, "output": "ls output"})
    res = mw_disabled.on_tool_execution("terminal", {"command": "ls"}, {}, next_call)
    assert res == {"ok": True, "output": "ls output"}
    next_call.assert_called_once_with({"command": "ls"})

    # 2. Frontdesk enabled -> task tools allowed
    mw_frontdesk = SuperAgentToolMiddleware(frontdesk_enabled=True, memory_read_enabled=False)
    next_task = MagicMock(return_value={"task_id": "t_100"})
    res_task = mw_frontdesk.on_tool_execution("durable_submit", {"goal": "analyze"}, {}, next_task)
    assert res_task == {"task_id": "t_100"}
    next_task.assert_called_once()

    # 3. Frontdesk enabled -> execution tools denied without invoking next_call
    next_denied = MagicMock()
    res_denied = mw_frontdesk.on_tool_execution("terminal", {"command": "rm -rf /"}, {}, next_denied)
    assert res_denied["ok"] is False
    assert res_denied["error_type"] == "frontdesk_policy"
    assert res_denied["error"] == TOOL_DENIED
    next_denied.assert_not_called()

    # 4. Memory read gating
    res_mem_denied = mw_frontdesk.on_tool_execution("memory_search_l1", {"query": "q"}, {}, next_denied)
    assert res_mem_denied["ok"] is False

    mw_mem_enabled = SuperAgentToolMiddleware(frontdesk_enabled=True, memory_read_enabled=True)
    next_mem = MagicMock(return_value={"memories": []})
    res_mem_ok = mw_mem_enabled.on_tool_execution("memory_search_l1", {"query": "q"}, {}, next_mem)
    assert res_mem_ok == {"memories": []}
    next_mem.assert_called_once()


def test_tool_middleware_write_guard_c21():
    mw = SuperAgentToolMiddleware(frontdesk_enabled=False, write_guard_enabled=True)
    next_call = MagicMock(return_value={"written": True})

    # Safe file -> allowed
    safe_res = mw.on_tool_execution("write_file", {"path": "/workspace/result.json", "content": "{}"}, {}, next_call)
    assert safe_res == {"written": True}
    next_call.assert_called_once()

    # Protected file (.git metadata) -> blocked
    next_git = MagicMock()
    git_res = mw.on_tool_execution("write_file", {"path": "/repo/.git/config", "content": "bad"}, {}, next_git)
    assert git_res["ok"] is False
    assert git_res["error_type"] == "write_guard"
    assert git_res["error"] == WRITE_DENIED
    next_git.assert_not_called()

    # Protected file (.env credentials) -> blocked
    next_env = MagicMock()
    env_res = mw.on_tool_execution("patch", {"file_path": "/app/.env", "diff": "+TOKEN=123"}, {}, next_env)
    assert env_res["ok"] is False
    assert env_res["error_type"] == "write_guard"
    next_env.assert_not_called()

    # Protected file (verify script) -> blocked
    next_verify = MagicMock()
    verify_res = mw.on_tool_execution("write_file", {"TargetFile": "/app/verify.sh"}, {}, next_verify)
    assert verify_res["ok"] is False
    assert verify_res["error_type"] == "write_guard"
    next_verify.assert_not_called()


def test_model_middleware_routing_and_budget_c20():
    mw = SuperAgentModelMiddleware(
        pro_model="custom-pro-model",
        flash_model="custom-flash-model",
        max_token_budget=8192,
    )

    # 1. Simple query -> routes to flash model
    simple_req = {
        "model": "auto",
        "messages": [{"role": "user", "content": "What is 2+2?"}],
        "max_tokens": 16000,
    }
    simple_out = mw.on_llm_request(simple_req, simple_req)
    assert simple_out["request"]["model"] == "custom-flash-model"
    assert simple_out["request"]["max_tokens"] == 8192  # Capped to budget
    assert simple_out["reason"] == "low_latency_default"
    assert simple_out["source"] == "superagent_middleware"

    # 2. Complex coding query -> routes to pro model
    complex_req = {
        "model": "auto",
        "messages": [{
            "role": "user",
            "content": "def solve():\n    ```python\n    import unittest\n    class Test(unittest.TestCase): pass\n    ```",
        }],
    }
    complex_out = mw.on_llm_request(complex_req, complex_req)
    assert complex_out["request"]["model"] == "custom-pro-model"
    assert complex_out["reason"] == "complex_reasoning_or_code"

    # 3. Explicit pro request -> honors explicit choice
    explicit_req = {
        "model": "my-pro-reasoning",
        "messages": [{"role": "user", "content": "hello"}],
    }
    explicit_out = mw.on_llm_request(explicit_req, explicit_req)
    assert explicit_out["request"]["model"] == "custom-pro-model"
    assert explicit_out["reason"] == "explicit_pro_requested"


def test_register_superagent_middleware():
    ctx = MagicMock()
    config = {
        "frontdesk": {"enabled": True, "memory_read_enabled": True},
        "write_guard_enabled": True,
        "pro_model": "test-pro",
        "flash_model": "test-flash",
    }
    register_superagent_middleware(ctx, config)
    assert ctx.register_middleware.call_count == 3
    registered_kinds = [call.args[0] for call in ctx.register_middleware.call_args_list]
    assert "tool_execution" in registered_kinds
    assert "tool_request" in registered_kinds
    assert "llm_request" in registered_kinds
