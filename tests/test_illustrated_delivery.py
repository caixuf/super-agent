import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[1]
PATH = REPO / 'plugins/illustrated_delivery/__init__.py'
SPEC = importlib.util.spec_from_file_location('illustrated_delivery_test_plugin', PATH)
PLUGIN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLUGIN)


class Context:
    def __init__(self, **settings):
        self.settings, self.hooks = settings, {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_hook(self, name, callback):
        self.hooks[name] = callback


def test_default_disabled_registers_nothing():
    context = Context()
    PLUGIN.register(context)
    assert context.hooks == {}


def test_hook_adds_temporary_advice_without_rendering(tmp_path):
    context = Context(enabled=True)
    PLUGIN.register(context)
    result = context.hooks['pre_llm_call'](user_message='介绍一下服务架构', session_id='test',
                                           task_id='test', turn_id='test', platform='api')
    assert set(result) == {'context'}
    assert 'illustrated-doc-rendering' in result['context']
    assert 'first use' in result['context']
    assert list(tmp_path.iterdir()) == []


def test_delegated_turns_do_not_duplicate_delivery():
    context = Context(enabled=True)
    PLUGIN.register(context)
    assert context.hooks['pre_llm_call'](user_message='架构', parent_session_id='parent') is None


def test_multimodal_request_gets_semantic_judgment_guidance():
    context = Context(enabled=True)
    PLUGIN.register(context)
    result = context.hooks['pre_llm_call'](user_message=[{'type': 'text', 'text': 'Explain this'}])
    assert 'decide whether' in result['context']


def test_engine_can_load_from_trusted_admin_source_when_package_unavailable(monkeypatch):
    import builtins
    original = builtins.__import__
    def unavailable(name, *args, **kwargs):
        if name == 'agentcore.document_intent':
            error = ModuleNotFoundError('not installed')
            error.name = 'agentcore'
            raise error
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', unavailable)
    context = Context(enabled=True, engine_root=str(REPO))
    PLUGIN.register(context)
    result = context.hooks['pre_llm_call'](user_message='介绍架构')['context']
    assert 'illustrated-doc-rendering' in result
    assert str(REPO / 'tools/arch_doc.py') in result
    assert str(REPO / 'skills/illustrated-doc-rendering/SKILL.md') in result
    assert 'Keep internal installation paths out' in result


def test_real_hermes_plugin_manager_hook(tmp_path):
    host = Path(os.environ.get('HERMES_SOURCE', Path.home() / '.hermes/hermes-agent'))
    if not (host / 'hermes_cli/plugins.py').is_file():
        pytest.skip('Hermes source unavailable')
    code = '''
import importlib.util,sys
sys.path.insert(0,sys.argv[1])
from hermes_cli.plugins import PluginContext,PluginManager,PluginManifest
from pathlib import Path
spec=importlib.util.spec_from_file_location('isolated_illustrated',sys.argv[3])
p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
manifest=PluginManifest(name='illustrated-delivery',version='1.0.0',description='test',author='test',path=Path(sys.argv[3]).parent)
manager=PluginManager(scope_key=sys.argv[4])
ctx=PluginContext(manifest,manager)
ctx.get_config=lambda key,default=None: {'enabled':True,'engine_root':sys.argv[2]}.get(key,default)
p.register(ctx)
assert len(manager._hooks['pre_llm_call'])==1
out=manager.invoke_hook('pre_llm_call',user_message='Explain the request lifecycle',session_id='test',task_id='test',turn_id='test',parent_session_id='',sender_id='test')
assert len(out)==1 and 'illustrated-doc-rendering' in out[0]['context']
assert str(Path(sys.argv[2])/'tools/arch_doc.py') in out[0]['context']
assert not manager._system_prompt_sections and not manager._plugin_tool_names
print('real hook registration passed')
'''
    runtime = host / '.venv/bin/python'
    interpreter = str(runtime) if runtime.exists() else sys.executable
    result = subprocess.run([interpreter, '-c', code, str(host), str(REPO), str(PATH), str(tmp_path / 'home')],
                            env=dict(os.environ, HERMES_HOME=str(tmp_path / 'home')),
                            capture_output=True, text=True, timeout=60)
    # A missing third-party module (e.g. ruamel.yaml) in the selected interpreter means the
    # Hermes tree cannot be imported at all: that is an environment gap, not a hook failure.
    missing = [line for line in (result.stderr or "").splitlines()
               if line.startswith("ModuleNotFoundError: No module named")]
    known_hermes_modules = ("hermes_cli", "gateway", "agent", "tools", "hermes_constants", "pm")
    if (result.returncode != 0 and missing
            and not any(name.split("'")[1].split(".")[0] in known_hermes_modules
                        for name in missing if "'" in name)):
        pytest.skip("Hermes runtime lacks a dependency: " + missing[-1])
    assert result.returncode == 0, result.stderr[-3000:]
    assert 'real hook registration passed' in result.stdout
