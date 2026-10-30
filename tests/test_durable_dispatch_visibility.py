from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
HOST = Path(os.environ.get("HERMES_SOURCE", Path.home() / ".hermes" / "hermes-agent"))
PATCH = REPO / "patches" / "patch_durable_dispatch_visibility.py"
VERIFY = REPO / "patches" / "verify_durable_dispatch_visibility.py"


def _stage_patched_host(tmp_path: Path) -> tuple[Path, Path]:
    if not (HOST / "tools" / "tool_search.py").is_file():
        pytest.skip("Hermes source unavailable for direct-visibility integration")
    staged_host = tmp_path / "patched-hermes"
    staged_tools = staged_host / "tools"
    staged_tools.mkdir(parents=True)
    shutil.copy2(HOST / "tools" / "tool_search.py", staged_tools / "tool_search.py")
    applied = subprocess.run(
        [sys.executable, str(PATCH), "--apply", "--agent-dir", str(staged_host)],
        capture_output=True, text=True, timeout=15,
    )
    assert applied.returncode == 0, applied.stdout + applied.stderr
    checked = subprocess.run(
        [sys.executable, str(PATCH), "--check", "--agent-dir", str(staged_host)],
        capture_output=True, text=True, timeout=15,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    verified = subprocess.run(
        [sys.executable, str(VERIFY), "--agent-dir", str(staged_host)],
        capture_output=True, text=True, timeout=15,
    )
    assert verified.returncode == 0, verified.stdout + verified.stderr

    # Overlay just tool_search.py and delegate every other Hermes tool module to the
    # installed source tree. This exercises the patch without editing the host checkout.
    overlay = tmp_path / "overlay"
    overlay_tools = overlay / "tools"
    overlay_tools.mkdir(parents=True)
    for item in (HOST / "tools").iterdir():
        if item.name == "tool_search.py":
            continue
        os.symlink(item, overlay_tools / item.name, target_is_directory=item.is_dir())
    shutil.copy2(staged_tools / "tool_search.py", overlay_tools / "tool_search.py")
    return staged_host, overlay


def test_direct_visibility_patch_is_scoped_and_idempotent(tmp_path):
    staged, _overlay = _stage_patched_host(tmp_path)
    target = staged / "tools" / "tool_search.py"
    text = target.read_text()
    assert text.count('{"durable_dispatch"}') == 1
    second = subprocess.run(
        [sys.executable, str(PATCH), "--apply", "--agent-dir", str(staged)],
        capture_output=True, text=True, timeout=15,
    )
    assert second.returncode == 0, second.stdout + second.stderr
    assert target.read_text() == text


def test_real_registry_exposes_only_enabled_durable_tools_directly(tmp_path):
    staged, overlay = _stage_patched_host(tmp_path)
    runtime = HOST / "venv" / "bin" / "python" if (HOST / "venv" / "bin" / "python").is_file() else HOST / ".venv" / "bin" / "python"
    if not runtime.is_file():
        pytest.skip("Hermes host interpreter unavailable")
    plugin_path = REPO / "plugins" / "durable_dispatch" / "__init__.py"
    home = tmp_path / "isolated-home"
    home.mkdir()
    code = r''' 
import importlib.util, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
sys.path.insert(1, sys.argv[2])
sys.path.insert(2, sys.argv[3])
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from hermes_cli.plugins_state import save_plugin_setting
from model_tools import get_tool_definitions
from tools.tool_search import assemble_tool_defs, classify_tools, ToolSearchConfig
home=Path(sys.argv[4])
for key,value in {'enabled':True,'engine_root':sys.argv[3],
                  'queue_root':str(home/'queue'),'allowed_roots':[str(home)]}.items():
    save_plugin_setting('durable-dispatch',(key,),value)
spec=importlib.util.spec_from_file_location('isolated_durable_visibility_plugin', sys.argv[5])
p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
manifest=PluginManifest(name='durable-dispatch',version='0.1.0',description='test',author='test',path=Path(sys.argv[5]).parent)
manager=PluginManager(scope_key=str(home)); ctx=PluginContext(manifest,manager)
p.register(ctx)
assert set(manager._system_prompt_sections)=={'durable-dispatch-routing'}
raw=get_tool_definitions(enabled_toolsets=['durable_dispatch'],quiet_mode=True,skip_tool_search_assembly=True)
names={item['function']['name'] for item in raw}
assert names=={'durable_submit','durable_status','durable_cancel'}, sorted(names)
visible,deferred=classify_tools(raw)
assert {item['function']['name'] for item in visible}==names
assert deferred==[], [item['function']['name'] for item in deferred]
assembled=assemble_tool_defs(raw,config=ToolSearchConfig.from_raw({'enabled':'on'}))
assert {item['function']['name'] for item in assembled.tool_defs}==names
assert not any(item['function']['name'] in ('tool_search','tool_describe','tool_call') for item in assembled.tool_defs)
blocked=get_tool_definitions(enabled_toolsets=['durable_dispatch'],disabled_toolsets=['durable_dispatch'],quiet_mode=True,skip_tool_search_assembly=True)
assert blocked==[], [item['function']['name'] for item in blocked]
print('enabled=direct, disabled=absent, system_prompt=registered')
'''
    env = dict(os.environ, HERMES_HOME=str(home))
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [str(runtime), "-c", code, str(overlay), str(HOST), str(REPO), str(home), str(plugin_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    assert "enabled=direct, disabled=absent" in result.stdout
