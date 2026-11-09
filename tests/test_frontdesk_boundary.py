"""Profile authorization and real, isolated Hermes dispatch (no real tools/IM)."""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest

REPO = Path(__file__).resolve().parents[1]
HOST = Path(os.environ.get("HERMES_SOURCE", Path.home() / ".hermes/hermes-agent"))
PATCH = REPO / "patches/patch_frontdesk_boundary.py"
HELPER = REPO / "runtime/hermes_frontdesk_policy.py"
HOST_FILES = ("model_tools.py", "agent/tool_executor.py",
              "agent/agent_runtime_helpers.py", "gateway/run_inbound.py",
              "agent/turn_tool_round.py", "agent/turn_finalizer.py", "run_agent.py")


@pytest.fixture
def policy():
    spec = importlib.util.spec_from_file_location("frontdesk_policy_test", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _enable(home, *, memory=False):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(json.dumps({
        "superagent": {"frontdesk": {"enabled": True, "memory_read_enabled": memory}},
    }))


@pytest.mark.parametrize("name", [
    "terminal", "execute_code", "delegate_task", "colleague_assign",
    "colleague_task_status", "colleague_catalog", "write_file", "read_file",
    "patch", "process", "tool_call", "tool_search", "manage_connections",
    "connectors__service__run", "unknown_plugin_tool", "memory", "cronjob",
])
def test_frontdesk_denies_execution_and_indirect_entries(policy, tmp_path, name):
    _enable(tmp_path)
    assert policy.tool_block_message(name, home=tmp_path) == policy.TOOL_DENIED


def test_frontdesk_exposes_task_apis_without_creating_extra_capabilities(policy, tmp_path):
    _enable(tmp_path)
    names = {"durable_submit", "durable_status", "durable_cancel", "durable_roster",
             "task_status", "colleague_list", "colleague_assign", "colleague_task_status",
             "lan_node_list", "lan_node_probe",
             "terminal", "memory_search_l1", "not_registered"}
    definitions = [{"type": "function", "function": {"name": name}} for name in names]
    filtered = policy.filter_tool_definitions(definitions, home=tmp_path)
    assert {item["function"]["name"] for item in filtered} == policy.TASK_TOOLS
    for name in policy.TASK_TOOLS:
        assert policy.tool_block_message(name, home=tmp_path) is None
    assert {item["function"]["name"] for item in definitions} == names
    assert policy.filter_tool_definitions([], home=tmp_path) == []


def test_memory_reads_need_explicit_opt_in_and_never_enable_writes(policy, tmp_path):
    _enable(tmp_path)
    for name in policy.MEMORY_READ_TOOLS:
        assert policy.tool_block_message(name, home=tmp_path) == policy.TOOL_DENIED
    _enable(tmp_path, memory=True)
    for name in policy.MEMORY_READ_TOOLS:
        assert policy.tool_block_message(name, home=tmp_path) is None
    assert policy.tool_block_message("memory", home=tmp_path) == policy.TOOL_DENIED


@pytest.mark.parametrize("content", [
    "superagent: [",
    "superagent: null",
    "superagent:\n  frontdesk:\n    enabled: 'true'\n",
    "superagent:\n  frontdesk:\n    enabled: true\n    enabled: false\n",
    "superagent:\n  frontdesk:\n    enabled: true\n    allowed_tools: ['terminal']\n",
    "superagent:\n  frontdesk:\n    memory_read_enabled: 1\n",
    "not-a-config-mapping",
])
def test_bad_policy_fails_closed_without_echoing_config(policy, tmp_path, content):
    (tmp_path / "config.yaml").write_text(content)
    assert policy.tool_block_message("durable_submit", home=tmp_path) == policy.UNAVAILABLE
    assert policy.filter_tool_definitions([{"function": {"name": "durable_submit"}}],
                                          home=tmp_path) == []
    assert policy.command_block_message("help", home=tmp_path) == policy.UNAVAILABLE


def test_worker_profile_is_unchanged_and_policy_does_not_leak_between_profiles(policy, tmp_path):
    desk, worker = tmp_path / "desk", tmp_path / "worker"
    _enable(desk)
    worker.mkdir()
    (worker / "config.yaml").write_text("superagent:\n  frontdesk:\n    enabled: false\n")
    for _ in range(2):
        assert policy.tool_block_message("terminal", home=desk) == policy.TOOL_DENIED
        assert policy.tool_block_message("terminal", home=worker) is None
    assert policy.tool_block_message("terminal", home=tmp_path / "missing-config") is None


def test_policy_matches_host_yaml_booleans_without_an_undeclared_pyyaml_dependency(policy, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)
    (tmp_path / "config.yaml").write_text(
        "superagent:\n  frontdesk:\n    enabled: y\n    memory_read_enabled: n\n"
    )
    assert policy.tool_block_message("durable_submit", home=tmp_path) is None
    assert policy.tool_block_message("terminal", home=tmp_path) == policy.TOOL_DENIED
    assert policy.tool_block_message("memory_search_l1", home=tmp_path) == policy.TOOL_DENIED


def test_oversized_profile_config_is_refused(policy, tmp_path):
    (tmp_path / "config.yaml").write_bytes(b" " * (1024 * 1024 + 1))
    assert policy.tool_block_message("durable_submit", home=tmp_path) == policy.UNAVAILABLE


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_nonregular_profile_config_is_refused_without_waiting(policy, tmp_path, kind):
    path = tmp_path / "config.yaml"
    if kind == "directory":
        path.mkdir()
    else:
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO is not supported on this platform")
        os.mkfifo(path)
    assert policy.tool_block_message("durable_submit", home=tmp_path) == policy.UNAVAILABLE


def test_commands_cannot_escape_through_quick_aliases_or_skill_commands(policy, tmp_path):
    _enable(tmp_path)
    assert policy.command_block_message(None, home=tmp_path) is None
    for command in policy.CHAT_COMMANDS:
        assert policy.command_block_message(command, home=tmp_path) is None
    for command in ("exec", "shell", "custom_alias", "custom_skill", "model", "profile",
                    "background", "approve", "reload", "new unsafe"):
        assert policy.command_block_message(command, home=tmp_path) == policy.COMMAND_DENIED


@pytest.mark.parametrize("platform", ["api_server", "local", "cli"])
def test_backend_session_allows_execution_tools_and_blocks_recursive_durable_submit(
    policy, tmp_path, monkeypatch, platform,
):
    _enable(tmp_path)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", platform)
    assert policy.tool_block_message("terminal", home=tmp_path) is None
    assert policy.tool_block_message("durable_submit", home=tmp_path) == policy.TOOL_DENIED
    definitions = [
        {"type": "function", "function": {"name": "terminal"}},
        {"type": "function", "function": {"name": "durable_submit"}},
        {"type": "function", "function": {"name": "durable_status"}},
    ]
    filtered = policy.filter_tool_definitions(definitions, home=tmp_path)
    assert [item["function"]["name"] for item in filtered] == ["terminal", "durable_status"]


def _stage_host(tmp_path, revision=None):
    if not all((HOST / name).is_file() for name in HOST_FILES):
        pytest.skip("Hermes source unavailable for frontdesk integration")
    staged = tmp_path / "host"
    staged.mkdir()
    # A symlink overlay is not isolation: Path(__file__).resolve() in bootstrap
    # and recovery code would still identify the operator's live checkout.
    result = subprocess.run(["git", "-C", str(HOST), "archive", revision or "HEAD"],
                            capture_output=True)
    assert result.returncode == 0, "Hermes source revision unavailable"
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        archive.extractall(staged, filter="data")
    if revision is None:
        # Preserve current tracked repairs without copying live install state,
        # backups, local secrets or dependency environments into the fixture.
        changed = subprocess.run(["git", "-C", str(HOST), "diff", "--name-only", "-z", "HEAD"],
                                 capture_output=True, check=True)
        for name in changed.stdout.decode().split("\0"):
            if not name:
                continue
            source, target = HOST / name, staged / name
            if source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            elif not source.exists() and target.exists():
                target.unlink()
    return staged


def _apply(staged):
    result = subprocess.run([sys.executable, str(PATCH), "--apply", "--agent-dir", str(staged)],
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def _host_run(staged, tmp_path, program):
    runtime = Path(os.environ.get("SUPERAGENT_HOST_TEST_PYTHON", HOST / "venv/bin/python"))
    if not runtime.is_file():
        pytest.skip("Hermes host interpreter unavailable")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ, HOME=str(home), HERMES_HOME=str(home),
               HERMES_ENABLE_PROJECT_PLUGINS="0", PYTHONDONTWRITEBYTECODE="1",
               HERMES_DISABLE_LAZY_INSTALLS="1", HERMES_TEST_ISOLATION=str(home))
    for name in tuple(env):
        if name.startswith(("FEISHU_", "HERMES_FEISHU_CARD_", "HFC_", "__HERMES_")) or name in (
                "PYTHONPATH", "HERMES_PROFILE", "HERMES_CONFIG", "HERMES_ENV",
                "HERMES_RUNTIME_DIR", "HERMES_PYTHON_SRC_ROOT"):
            env.pop(name, None)
    code = """import sys, socket
sys.path.insert(0, sys.argv[1])
def no_network(*args, **kwargs):
    raise AssertionError("frontdesk integration tests must not open network connections")
socket.socket.connect = no_network
socket.socket.connect_ex = no_network
""" + program + """
from pathlib import Path
snapshot=Path(sys.argv[1]).resolve()
for module_name in ("model_tools","gateway.run","agent.tool_executor","hermes_bootstrap"):
    module=sys.modules.get(module_name)
    if module is not None:
        assert Path(module.__file__).resolve().is_relative_to(snapshot), module_name
"""
    result = subprocess.run([str(runtime), "-c", code, str(staged), str(home), str(REPO)],
                            env=env, cwd=tmp_path, text=True, capture_output=True, timeout=90)
    assert result.returncode == 0, (result.stdout + result.stderr)[-6000:]
    return result.stdout


@pytest.mark.parametrize("revision", [None, "bf0203a794a07aac3ff764972ab9087baa4e4f4e"],
                         ids=["current-host", "pinned-candidate"])
def test_real_host_direct_and_bridge_dispatch_cannot_execute_frontdesk_tools(tmp_path, revision):
    staged = _stage_host(tmp_path, revision)
    _apply(staged)
    output = _host_run(staged, tmp_path, r'''
import json
from pathlib import Path
from unittest.mock import patch
home=Path(sys.argv[2])
(home/"config.yaml").write_text("superagent:\n  frontdesk:\n    enabled: true\n")
import model_tools
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from agent.secret_scope import set_multiplex_context, reset_multiplex_context
scope=set_hermes_home_override(home)
try:
    with patch.object(model_tools.registry, "dispatch", return_value='{"executed":true}') as dispatch:
        for name in ("terminal", "execute_code", "write_file", "delegate_task", "tool_call",
                     "manage_connections", "connectors__service__run", "unknown_plugin_tool"):
            answer=model_tools.handle_function_call(name, {}, session_id="trusted")
            assert "frontdesk_" in answer, (name, answer)
        assert dispatch.call_count==0
        assert "executed" in model_tools.handle_function_call("durable_submit", {}, session_id="trusted")
        assert dispatch.call_count==1
    # Removing the hook infrastructure cannot bypass the independent policy gate.
    with patch("hermes_cli.plugins._dispatch_pre_tool_call_hooks", side_effect=RuntimeError("hook unavailable")):
        with patch.object(model_tools.registry, "dispatch") as dispatch:
            assert "frontdesk_" in model_tools.handle_function_call("terminal", {})
            dispatch.assert_not_called()
    # Cached worker schemas must not become the frontdesk schema surface.
    schema=[{"type":"function","function":{"name":name}} for name in ("terminal","durable_submit")]
    model_tools._clear_tool_defs_cache()
    with patch.object(model_tools, "_compute_tool_definitions", return_value=schema):
        assert {x["function"]["name"] for x in model_tools.get_tool_definitions(quiet_mode=True)}=={"durable_submit"}
    multi=set_multiplex_context(True)
    reset_hermes_home_override(scope)
    scope=None
    try:
        with patch.object(model_tools.registry, "dispatch") as dispatch:
            assert "frontdesk_policy_unavailable" in model_tools.handle_function_call("terminal", {})
            dispatch.assert_not_called()
    finally:
        reset_multiplex_context(multi)
finally:
    if scope is not None:
        reset_hermes_home_override(scope)
print("frontdesk-direct-dispatch-passed")
''')
    assert "frontdesk-direct-dispatch-passed" in output


def test_agent_inline_and_authorized_dispatch_enforce_policy_when_hooks_are_skipped(tmp_path):
    staged = _stage_host(tmp_path)
    _apply(staged)
    output = _host_run(staged, tmp_path, r'''
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
home=Path(sys.argv[2])
(home/"config.yaml").write_text("superagent:\n  frontdesk:\n    enabled: true\n")
from agent import tool_executor as executor
from agent.agent_runtime_helpers import invoke_tool
agent=SimpleNamespace(session_id="trusted")
executed=Mock(return_value='{"executed":true}')
post=Mock()
for name in ("memory","todo","delegate_task","terminal","execute_code","unknown_plugin_tool"):
    ref=SimpleNamespace(name=name, args={}, emit_post=post)
    state=SimpleNamespace(blocked=False,args={})
    result=executor._dispatch_authorized_once(
        agent,state,ref,execute=executed,scope_block=None,display_index=None,
        begin_execution=None,authorization_gate=None)
    assert state.blocked and "frontdesk_tool_denied" in result, (name,result)
    # This entry can be called outside the normal Agent loop, with prechecked flags.
    assert "frontdesk_tool_denied" in invoke_tool(
        agent,name,{},"task",pre_tool_block_checked=True,skip_tool_request_middleware=True,
        skip_tool_execution_middleware=True)
executed.assert_not_called()
assert post.call_count==6
print("frontdesk-agent-boundaries-passed")
''')
    assert "frontdesk-agent-boundaries-passed" in output


def test_im_command_gate_runs_before_plugins_busy_routing_and_exec_quick_commands(tmp_path):
    staged = _stage_host(tmp_path)
    _apply(staged)
    output = _host_run(staged, tmp_path, r'''
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
home=Path(sys.argv[2])
# Ambient launch profile is a Worker. The admitted event routes to the desk.
(home/"config.yaml").write_text("superagent:\n  frontdesk:\n    enabled: false\n")
desk=home/"routed-desk"
desk.mkdir()
(desk/"config.yaml").write_text("superagent:\n  frontdesk:\n    enabled: true\n")
from gateway.run_inbound import GatewayInboundMixin
source=SimpleNamespace()
event=SimpleNamespace(get_command=lambda:"run_shell")
runner=SimpleNamespace(
    _hm_admit_event=AsyncMock(return_value=(event,source,False)),
    _resolve_profile_home_for_source=Mock(return_value=desk),
    _hm_pending_reply_intercepts=AsyncMock(),
    _hm_dispatch_idle_commands=AsyncMock(),
    _hm_handle_running_session_message=AsyncMock(),
)
answer=asyncio.run(GatewayInboundMixin._handle_message(runner,event))
assert "frontdesk_command_denied" in answer, answer
runner._resolve_profile_home_for_source.assert_called_once_with(source)
runner._hm_dispatch_idle_commands.assert_not_called()
runner._hm_handle_running_session_message.assert_not_called()
runner._hm_pending_reply_intercepts.assert_not_called()
print("frontdesk-im-command-boundary-passed")
''')
    assert "frontdesk-im-command-boundary-passed" in output


@pytest.mark.parametrize("fault", ["missing", "import-error", "call-error"])
def test_policy_helper_failure_cannot_reach_any_execution_entry(tmp_path, fault):
    staged = _stage_host(tmp_path)
    _apply(staged)
    helper = staged / "superagent_frontdesk.py"
    if fault == "missing":
        helper.unlink()
    elif fault == "import-error":
        helper.write_text("raise RuntimeError('private configuration detail')\n")
    else:
        helper.write_text(
            "def unavailable(*args, **kwargs):\n"
            "    raise RuntimeError('private configuration detail')\n"
            "tool_block_message = command_block_message = filter_tool_definitions = unavailable\n"
        )
    output = _host_run(staged, tmp_path, r'''
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from pathlib import Path
import model_tools
from agent import tool_executor as executor
from agent.agent_runtime_helpers import invoke_tool
from gateway.run_inbound import GatewayInboundMixin
agent=SimpleNamespace(session_id="trusted")
executed=Mock()
with patch.object(model_tools.registry, "dispatch") as dispatch:
    answer=model_tools.handle_function_call("durable_submit", {})
    assert "frontdesk_policy_unavailable" in answer and "private" not in answer
    dispatch.assert_not_called()
with patch.object(model_tools, "_compute_tool_definitions",
                  return_value=[{"function":{"name":"durable_submit"}}]):
    assert model_tools.get_tool_definitions()==[]
ref=SimpleNamespace(name="durable_submit",args={},emit_post=Mock())
state=SimpleNamespace(blocked=False,args={})
answer=executor._dispatch_authorized_once(agent,state,ref,execute=executed,
    scope_block=None,display_index=None,begin_execution=None,authorization_gate=None)
assert state.blocked and "frontdesk_policy_unavailable" in answer and "private" not in answer
executed.assert_not_called()
answer=invoke_tool(agent,"durable_submit",{},"task",pre_tool_block_checked=True,
    skip_tool_request_middleware=True,skip_tool_execution_middleware=True)
assert "frontdesk_policy_unavailable" in answer and "private" not in answer
event=SimpleNamespace(get_command=lambda:"help")
runner=SimpleNamespace(
    _hm_admit_event=AsyncMock(return_value=(event,object(),False)),
    _resolve_profile_home_for_source=Mock(return_value=Path(sys.argv[2])),
    _hm_pending_reply_intercepts=AsyncMock())
answer=asyncio.run(GatewayInboundMixin._handle_message(runner,event))
assert "frontdesk_policy_unavailable" in answer and "private" not in answer
runner._hm_pending_reply_intercepts.assert_not_called()
print("frontdesk-helper-failure-closed")
''')
    assert "frontdesk-helper-failure-closed" in output


def test_real_dispatch_fails_closed_for_malformed_config(tmp_path):
    staged = _stage_host(tmp_path)
    _apply(staged)
    output = _host_run(staged, tmp_path, r'''
from pathlib import Path
from unittest.mock import patch
import model_tools
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
home=Path(sys.argv[2])
(home/"config.yaml").write_text("superagent:\n  frontdesk:\n    enabled: private-invalid-value\n")
scope=set_hermes_home_override(home)
try:
    with patch.object(model_tools.registry, "dispatch") as dispatch:
        for name in ("terminal","durable_submit"):
            answer=model_tools.handle_function_call(name,{})
            assert "frontdesk_policy_unavailable" in answer and "private-invalid" not in answer
        dispatch.assert_not_called()
finally:
    reset_hermes_home_override(scope)
print("frontdesk-malformed-config-closed")
''')
    assert "frontdesk-malformed-config-closed" in output


def test_concurrent_worker_and_frontdesk_scopes_and_warm_schema_cache_stay_isolated(tmp_path):
    staged = _stage_host(tmp_path)
    _apply(staged)
    output = _host_run(staged, tmp_path, r'''
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
import model_tools
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from agent.secret_scope import set_multiplex_context, reset_multiplex_context
root=Path(sys.argv[2])
desk,worker=root/"desk",root/"worker"
for home,enabled in ((desk,"true"),(worker,"false")):
    home.mkdir()
    (home/"config.yaml").write_text("superagent:\n  frontdesk:\n    enabled: "+enabled+"\n")
schema=[{"type":"function","function":{"name":name}} for name in ("terminal","durable_submit")]
model_tools._clear_tool_defs_cache()
with patch.object(model_tools,"_tool_defs_cache_key",return_value=("shared-cache",)), \
     patch.object(model_tools,"_compute_tool_definitions",return_value=schema) as compute:
    # Deliberately share a warmed raw cache entry; policy still runs per caller.
    scope=set_hermes_home_override(worker)
    try:
        assert len(model_tools.get_tool_definitions(quiet_mode=True))==2
    finally:
        reset_hermes_home_override(scope)
    barrier=Barrier(2)
    def run(home,restricted):
        multi=set_multiplex_context(True)
        scope=set_hermes_home_override(home)
        try:
            for _ in range(10):
                barrier.wait(timeout=10)
                answer=model_tools.handle_function_call("terminal",{})
                assert ("frontdesk_tool_denied" in answer) is restricted,answer
                names={x["function"]["name"] for x in model_tools.get_tool_definitions(quiet_mode=True)}
                assert names==({"durable_submit"} if restricted else {"terminal","durable_submit"}),names
        finally:
            reset_hermes_home_override(scope)
            reset_multiplex_context(multi)
    with patch.object(model_tools.registry,"dispatch",return_value='{"executed":true}') as dispatch:
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures=[workers.submit(run,desk,True),workers.submit(run,worker,False)]
            for future in futures:
                future.result(timeout=30)
        assert dispatch.call_count==10
    compute.assert_called_once()
print("frontdesk-concurrent-profiles-isolated")
''')
    assert "frontdesk-concurrent-profiles-isolated" in output


def test_patch_is_idempotent_and_check_detects_missing_helper(tmp_path):
    staged = _stage_host(tmp_path)
    _apply(staged)
    before = {name: (staged / name).read_bytes() for name in HOST_FILES}
    _apply(staged)
    assert before == {name: (staged / name).read_bytes() for name in HOST_FILES}
    checked = subprocess.run([sys.executable, str(PATCH), "--check", "--agent-dir", str(staged)],
                             capture_output=True, text=True)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    (staged / "superagent_frontdesk.py").unlink()
    checked = subprocess.run([sys.executable, str(PATCH), "--check", "--agent-dir", str(staged)],
                             capture_output=True, text=True)
    assert checked.returncode == 1


def test_candidate_installer_applies_and_probes_frontdesk_with_all_kept_patches(tmp_path):
    staged = _stage_host(tmp_path, "bf0203a794a07aac3ff764972ab9087baa4e4f4e")
    spec = importlib.util.spec_from_file_location("frontdesk_installer_test", REPO / "tools/superagent.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    assert next(item for item in installer._keep_patch_state(staged)
                if item["id"] == "frontdesk-boundary")["in_place"] is False
    applied = installer._apply_keep_patches(staged)
    expected = {item[1] for item in installer.KEEP_HOST_PATCH_TOOLS}
    assert {item["id"] for item in applied} == expected
    assert all(item["state"] in {"applied", "present"} for item in applied), applied
    probed = installer._keep_patch_state(staged)
    assert all(item["in_place"] for item in probed), probed
    assert all(item["state"] == "present" for item in installer._apply_keep_patches(staged))
    assert (staged / "superagent_frontdesk.py").read_bytes() == HELPER.read_bytes()
    assert not (staged / "config.yaml").exists()


def test_patch_anchor_drift_refuses_all_writes(tmp_path):
    staged = _stage_host(tmp_path)
    (staged / "gateway/run_inbound.py").write_text("# unsupported host\n")
    before = {name: (staged / name).read_bytes() for name in HOST_FILES}
    applied = subprocess.run([sys.executable, str(PATCH), "--apply", "--agent-dir", str(staged)],
                             capture_output=True, text=True)
    assert applied.returncode == 2
    assert before == {name: (staged / name).read_bytes() for name in HOST_FILES}
    assert not (staged / "superagent_frontdesk.py").exists()


@pytest.mark.parametrize("fault", ["symlink-parent", "invalid-source", "nonregular-helper"])
def test_patch_unsafe_targets_and_invalid_source_refuse_before_writes(tmp_path, fault):
    staged = _stage_host(tmp_path)
    if fault == "symlink-parent":
        outside = tmp_path / "outside"
        (staged / "agent").rename(outside)
        (staged / "agent").symlink_to(outside, target_is_directory=True)
    elif fault == "invalid-source":
        target = staged / "gateway/run_inbound.py"
        target.write_text(target.read_text() + "\ndef broken(:\n")
    else:
        (staged / "superagent_frontdesk.py").mkdir()
    before = {name: (staged / name).read_bytes() for name in HOST_FILES}
    applied = subprocess.run([sys.executable, str(PATCH), "--apply", "--agent-dir", str(staged)],
                             capture_output=True, text=True)
    assert applied.returncode == 2, applied.stdout + applied.stderr
    assert before == {name: (staged / name).read_bytes() for name in HOST_FILES}
    assert not (staged / "superagent_frontdesk.py").is_file()


@pytest.mark.parametrize("revision", [None, "bf0203a794a07aac3ff764972ab9087baa4e4f4e"],
                         ids=["current-host", "pinned-candidate"])
def test_frontdesk_cannot_spawn_or_resume_memory_skill_review(tmp_path, revision):
    staged = _stage_host(tmp_path, revision)
    _apply(staged)
    _host_run(staged, tmp_path, r'''
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock,patch
from hermes_constants import set_hermes_home_override,reset_hermes_home_override
from run_agent import AIAgent
home=Path(sys.argv[2])
scope=set_hermes_home_override(home)
agent=AIAgent.__new__(AIAgent)
agent._spawn_background_review_now=MagicMock()
try:
    for state in ("enabled","worker","malformed","helper-failed","helper-missing"):
        (home/"config.yaml").write_text("superagent:\n  frontdesk:\n    enabled: "+
            ("false" if state=="worker" else "true")+"\n    memory_read_enabled: true\n")
        if state=="malformed":
            (home/"config.yaml").write_text("superagent: [")
        def broken(**kwargs): raise ValueError("private config detail")
        helper={}
        if state=="helper-failed":
            helper={"superagent_frontdesk":SimpleNamespace(tool_block_message=broken)}
        elif state=="helper-missing":
            helper={"superagent_frontdesk":None}
        with patch.dict(sys.modules,helper), \
             patch("agent.background_review.load_background_review_settings",return_value=(True,{})) as settings, \
             patch("run_agent._review_should_defer",return_value=False), \
             patch("agent.background_review.prepare_background_review_run",return_value=None) as prepare:
            # Automatic, explicit and previously deferred invocations all use
            # the same policy before any model routing, queueing or thread.
            agent._spawn_background_review_now.reset_mock()
            AIAgent._spawn_background_review(agent,[],review_memory=True)
            AIAgent._spawn_background_review(agent,[],review_skills=True,focus="review",explicit=True)
            AIAgent._spawn_background_review_now(agent,[],review_memory=True)
            assert agent._spawn_background_review_now.call_count==(2 if state=="worker" else 0),state
            assert prepare.call_count==(1 if state=="worker" else 0),state
            assert settings.call_count==(1 if state=="worker" else 0),state
finally:reset_hermes_home_override(scope)
''')


@pytest.mark.parametrize("revision", [None, "bf0203a794a07aac3ff764972ab9087baa4e4f4e"],
                         ids=["current-host", "pinned-candidate"])
@pytest.mark.parametrize("scenario", ["accepted", "refused", "worker", "helper-failed",
                                     "result-persistence-failed"])
def test_real_agent_releases_turn_after_durable_submit_and_accepts_next_message(tmp_path, revision, scenario):
    staged = _stage_host(tmp_path, revision)
    _apply(staged)
    if scenario == "helper-failed":
        (staged / "superagent_frontdesk_turn.py").write_text("raise RuntimeError('private detail')\n")
    output = _host_run(staged, tmp_path, "scenario=" + repr(scenario) + "\n" + r'''
import importlib.util,json,os,threading,time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock,patch
home,repo=Path(sys.argv[2]),Path(sys.argv[3])
sys.path.insert(1,str(repo))
(home/"config.yaml").write_text("superagent:\n  frontdesk:\n    enabled: "+
                               ("false" if scenario=="worker" else "true")+"\n")
source=home/"evidence.log"
source.write_text("Synthetic failure evidence\n")
spec=importlib.util.spec_from_file_location("isolated_frontdesk_dispatch",repo/"plugins/durable_dispatch/__init__.py")
plugin=importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
from hermes_constants import set_hermes_home_override,reset_hermes_home_override
from tools.registry import registry
from gateway.session_context import set_session_vars,clear_session_vars
scope=set_hermes_home_override(home)
session_tokens=set_session_vars(platform="test-im",chat_id="chat-original",
    thread_id="thread-original",session_key="route-original",session_id="frontdesk-session")
class Context:
    profile_name="frontdesk-test"
    schemas=[]
    def get_config(self,key,default=None):
        return {"enabled":True,"queue_root":str(home/"queue"),"allowed_roots":[str(home)],
                "engine_root":str(repo),"liveness_db":str(home/"liveness.db")}.get(key,default)
    def register_system_prompt_section(self,*args,**kwargs): pass
    def register_tool(self,**kwargs):
        self.schemas.append({"type":"function","function":kwargs["schema"]})
        registry.register(**kwargs)
context=Context()
plugin.register(context)
from run_agent import AIAgent
def response(content=None,tool_calls=None):
    return SimpleNamespace(model="test/model",usage=None,choices=[
        SimpleNamespace(finish_reason="tool_calls" if tool_calls else "stop",
                        message=SimpleNamespace(content=content,tool_calls=tool_calls))])
args={"goal":"Analyze this bounded evidence","request_key":"same-request",
      "sources":[{"path":str(home/"config.yaml" if scenario=="refused" else source)}]}
call=SimpleNamespace(id="submit-1",type="function",
                     function=SimpleNamespace(name="durable_submit",arguments=json.dumps(args)))
try:
    with patch("model_tools.get_tool_definitions",return_value=context.schemas), \
         patch("model_tools.check_toolset_requirements",return_value={}), \
         patch("hermes_cli.plugins.discover_plugins",return_value=None), \
         patch("agent.process_bootstrap.OpenAI"):
        agent=AIAgent(api_key="test-key-1234567890",base_url="http://127.0.0.1:1/v1",
            model="test/model",provider="openai",api_mode="chat_completions",
            quiet_mode=True,skip_context_files=True,skip_memory=True,
            session_id="frontdesk-session",max_iterations=3 if scenario=="worker" else 1,
            save_trajectories=False)
    agent.client=MagicMock()
    agent._cached_system_prompt="You interact and submit durable tasks only."
    agent._use_prompt_caching=False
    agent.tool_delay=0
    agent.compression_enabled=False
    agent.client.chat.completions.create.side_effect=[
        response(tool_calls=[call]),response(content="BUG: the frontdesk called the model again")]
    if scenario=="result-persistence-failed":
        original_flush=agent._flush_messages_to_session_db
        def flush(messages,*args,**kwargs):
            if any(item.get("role")=="tool" for item in messages):
                return False
            return original_flush(messages,*args,**kwargs)
        agent._flush_messages_to_session_db=flush
    first=agent.run_conversation("Analyze the supplied evidence.")
    assert agent.client.chat.completions.create.call_count==(2 if scenario=="worker" else 1),first
    expected_failure=scenario in ("helper-failed","result-persistence-failed")
    assert first["completed"] is not expected_failure and first["failed"] is expected_failure,first
    if scenario!="result-persistence-failed":
        assert first["turn_exit_reason"]==("text_response(finish_reason=stop)" if scenario=="worker" else "frontdesk_handoff"),first
    pool=plugin.Pool(str(home/"queue"))
    with pool.db() as db:
        rows=db.execute("SELECT id,status FROM jobs").fetchall()
    assert len(rows)==(0 if scenario=="refused" else 1),rows
    if scenario=="refused":
        assert "未受理" in first["final_response"] and "任务没有创建" in first["final_response"],first
        assert "已持久记录" not in first["final_response"],first
        assert "拒绝原因：sensitive source path denied" in first["final_response"],first
    elif scenario=="helper-failed":
        assert "frontdesk_handoff_unavailable" in first["final_response"] and "private" not in first["final_response"],first
    elif scenario=="accepted":
        assert rows[0]["id"] in first["final_response"],first
    elif scenario=="result-persistence-failed":
        assert "已持久记录" not in first["final_response"],first
    assert first["messages"][-1]["role"]=="assistant"
    assert first["messages"][-1]["content"]==first["final_response"]
    # Run the actual bounded analysis subprocess, blocked only at the synthetic
    # model boundary. The foreground must accept chat while that task is running.
    if scenario=="accepted":
        from agentcore.worker import Worker
        from agentcore.ops_alerts import DeliveryReceipt
        from agentcore.completion_backfill import drain
        job=rows[0]["id"]
        origin={"profile":"frontdesk-test","session":"frontdesk-session",
                "route":"route-original","channel":"test-im",
                "chat":"chat-original","thread":"thread-original"}
        assert pool.origin(job)==origin,pool.origin(job)
        service=plugin.DispatchService(str(home/"queue"),"frontdesk-test",[str(home)],
            display_name="主 Bot 汇总",display_label="汇总",
            liveness_path=str(home/"liveness.db"))
        # Ownership gates apply even if a caller knows the task ID and queue.
        foreign_profile=plugin.DispatchService(str(home/"queue"),"another-profile",[str(home)],
            liveness_path=str(home/"foreign-liveness.db"))
        for outsider,session in ((service,"another-session"),(foreign_profile,"frontdesk-session")):
            for action in (outsider.status,outsider.cancel):
                try:action(session,{"task_id":job})
                except PermissionError:pass
                else:raise AssertionError("task exposed across trusted identity boundary")
        retry=service.submit("frontdesk-session",args)
        assert retry["task_id"]==job
        policy=home/"analysis-policy.json"
        policy.write_text(json.dumps({"allowed_roots":[str(home)]}))
        (home/"offline_analysis.py").write_text(
            "from pathlib import Path\nimport time\n"
            "def model(messages):\n"
            "    Path("+repr(str(home/"worker-entered"))+").touch()\n"
            "    deadline=time.monotonic()+15\n"
            "    while not Path("+repr(str(home/"worker-release"))+").exists():\n"
            "        if time.monotonic()>deadline:raise TimeoutError('test release absent')\n"
            "        time.sleep(.01)\n"
            "    return {'conclusion':'Synthetic failure recorded','claims':["
            "{'statement':'Evidence is present','evidence':[{'source_id':'source_1',"
            "'line':1,'quote':'Synthetic failure evidence'}]}],"
            "'limitations':['Offline fixture, not real-model acceptance']}\n")
        worker_errors=[]
        def work():
            try:
                Worker(pool,[sys.executable,str(repo/"agentcore/analysis_runner.py"),
                    "--policy",str(policy),"--model-adapter","offline_analysis:model"],
                    timeout=20,poll_interval=.02).run(drain=True)
            except BaseException as exc:worker_errors.append(type(exc).__name__)
        worker=threading.Thread(target=work)
        old_path=os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"]=str(home)+os.pathsep+str(repo)
        worker.start()
        try:
            deadline=time.monotonic()+10
            while not (home/"worker-entered").exists():
                assert worker.is_alive() and not worker_errors,worker_errors
                assert time.monotonic()<deadline,"analysis runner never entered model boundary"
                time.sleep(.01)
            assert pool.status(job)["status"]=="running"
            assert pool.receipts()==[]
            agent.client.chat.completions.create.reset_mock()
            agent.client.chat.completions.create.side_effect=[response(content="The chat is available.")]
            second=agent.run_conversation("Can we keep chatting?",conversation_history=first["messages"])
            assert second["completed"] is True,second
            assert second["final_response"]=="The chat is available.",second
            assert agent.client.chat.completions.create.call_count==1
            assert pool.status(job)["status"]=="running"
            with pool.db() as db:
                assert db.execute("SELECT count(*) FROM jobs").fetchone()[0]==1
        finally:
            (home/"worker-release").touch()
            worker.join(timeout=25)
            if old_path is None:os.environ.pop("PYTHONPATH",None)
            else:os.environ["PYTHONPATH"]=old_path
        assert not worker.is_alive() and not worker_errors,worker_errors
        # Reopen rather than inheriting in-memory task/delivery objects.
        pool=plugin.Pool(str(home/"queue"))
        assert pool.status(job)["status"]=="completed",pool.status(job)
        result=json.loads(pool.status(job)["result"])["result"]
        assert result["verification"]=="citation_traceability_only"
        assert all(Path(path).is_file() for path in result["artifacts"])
        set_session_vars(platform="test-im",chat_id="chat-other",thread_id="thread-other",
                         session_key="route-other",session_id="another-session")
        class Transport:
            calls=[]
            def deliver(self,address,message,*,idempotency_key):
                self.calls.append((address,message,idempotency_key))
                return DeliveryReceipt(True,idempotency_key,"offline-receipt")
        transport=Transport()
        assert drain(pool,transport,profile="another-profile")["delivered"]==0
        assert transport.calls==[]
        assert drain(pool,transport,profile="frontdesk-test")["delivered"]==1
        assert len(transport.calls)==1
        address,message,key=transport.calls[0]
        assert address==origin and key==job and message["task_id"]==job
        assert message["status"]=="completed" and message["route"]=="route-original"
        assert pool.delivery_record(job)["receipt_id"]=="offline-receipt"
        assert drain(plugin.Pool(str(home/"queue")),transport,profile="frontdesk-test")["delivered"]==0
        assert len(transport.calls)==1
    agent.close()
finally:
    clear_session_vars(session_tokens)
    reset_hermes_home_override(scope)
print("frontdesk-real-turn-release-passed")
''')
    assert "frontdesk-real-turn-release-passed" in output



# --- handoff physical finalize (EOS) -----------------------------------------

def _turn_helper():
    import importlib.util
    spec = importlib.util.spec_from_file_location("frontdesk_turn_test", REPO / "runtime/hermes_frontdesk_turn.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_finalize_after_release_emits_eos_to_stream_callback():
    calls = []
    agent = type("Agent", (), {"stream_delta_callback": lambda self, value: calls.append(value)})()
    assert _turn_helper().finalize_after_release(agent) is True
    assert calls == [None], "EOS (None) must close the display stream box exactly once"


def test_finalize_after_release_without_callback_is_a_noop():
    assert _turn_helper().finalize_after_release(object()) is False


def test_finalize_after_release_never_raises_on_broken_callback():
    def broken(_value):
        raise RuntimeError("display sink unavailable")
    agent = type("Agent", (), {"stream_delta_callback": broken})()
    assert _turn_helper().finalize_after_release(agent) is False


def test_frontdesk_release_hook_is_idempotent_and_migrates_v1(tmp_path):
    staged = _stage_host(tmp_path)
    _apply(staged)
    target = staged / "agent/turn_tool_round.py"
    after = target.read_text(encoding="utf-8")
    assert after.count("SUPERAGENT_FRONTDESK_TURN_RELEASE") == 1
    assert "finalize_after_release(agent)" in after
    _apply(staged)
    assert target.read_text(encoding="utf-8") == after, "re-applying must not duplicate the hook"
    checked = subprocess.run([sys.executable, str(PATCH), "--check", "--agent-dir", str(staged)],
                             capture_output=True, text=True)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    # Simulate a host carrying the pre-finalize V1 block, then re-apply.
    import importlib.util
    spec = importlib.util.spec_from_file_location("fd_patch_migration", PATCH)
    patch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(patch)
    assert patch._ROUND_RELEASE in after
    target.write_text(after.replace(patch._ROUND_RELEASE, patch._ROUND_RELEASE_V1, 1), encoding="utf-8")
    _apply(staged)
    migrated = target.read_text(encoding="utf-8")
    assert "finalize_after_release(agent)" in migrated
    assert migrated.count("SUPERAGENT_FRONTDESK_TURN_RELEASE") == 1, "migration must not insert a second hook"
    assert patch._ROUND_RELEASE in migrated

    target.write_text(after.replace(patch._ROUND_RELEASE, patch._ROUND_RELEASE_V2, 1), encoding="utf-8")
    _apply(staged)
    migrated_v2 = target.read_text(encoding="utf-8")
    assert "finalize_after_release(agent)" in migrated_v2
    assert migrated_v2.count("SUPERAGENT_FRONTDESK_TURN_RELEASE") == 1, "migration from v2 must not duplicate"
    assert patch._ROUND_RELEASE in migrated_v2

