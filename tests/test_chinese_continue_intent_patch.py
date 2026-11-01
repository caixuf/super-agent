"""Behavioral regression for the existing host stall guard's local-language gap."""
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

PATCH = Path(__file__).resolve().parents[1] / 'patches/patch_chinese_continue_intent.py'
spec = importlib.util.spec_from_file_location('chinese_continue_patch', PATCH)
patcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patcher)
PRISTINE = r'''import re
_TRAILING_CONTINUE_INTENT_RE = re.compile(r"(?:let me now|i will now)[^.!?\n]{0,100}[.:]?\s*$", re.I)
_TRAILING_CONTINUE_INTENT_MAX_CHARS = 400
def trailing_continue_intent(text: str) -> bool:
    t = (text or "").strip()
    if not t or len(t) > _TRAILING_CONTINUE_INTENT_MAX_CHARS:
        return False
    return bool(_TRAILING_CONTINUE_INTENT_RE.search(t[-160:]))
'''

def detector(source=PRISTINE):
    ns = {}
    exec(compile(patcher.patched_source(source), '<patched-host>', 'exec'), ns)
    return ns['trailing_continue_intent']

@pytest.mark.parametrize('text', [
    '检查报了一项失败。先查清是不是超时引发的。',
    '你说得对。现在我检查日志。',
    '接下来核对实际退出码。',
    '下面复现这个失败。',
    '先跑一遍回归。',
    '我先定位失败的位置。',
    'Found the config. Let me now update it.',
])
def test_action_tails_reuse_existing_classifier(text):
    assert detector()(text)

@pytest.mark.parametrize('text', [
    '全部检查通过。', '你好。', '42', '', None,
    '建议先查清是不是超时引发的。',
    '你可以先查清是不是超时引发的。',
    '请你先检查日志。',
    '“先查清是不是超时引发的。”',
    '> 先检查日志。',
    '```text\n先检查日志。\n```',
    '~~~text\n先检查日志。',
    '先检查日志。结果是通过。',
    '先检查日志需要你授权。',
    '现在检查前先等你确认。',
    '先查清是不是故障，确认后再处理。',
    '先查清原因是建议，不是已经执行。',
    '先检查日志，如果你批准的话。',
    '下一步由你检查日志。',
    '先检查日志已经完成。',
    '先检查日志已完成。',
    '先检查日志可以吗。',
    '先检查日志要等到明天。',
    '现在重启服务。',
    'x' * 401 + '。先检查日志。',
])
def test_normal_answers_advice_and_approval_waits_do_not_act(text):
    assert not detector()(text)

def test_patch_is_idempotent_and_preserves_surrounding_source():
    source = '# prior local change\n' + PRISTINE + '\nOTHER = 42\n'
    updated = patcher.patched_source(source)
    assert patcher.patched_source(updated) == updated
    assert updated.startswith('# prior local change\n')
    assert updated.endswith('\nOTHER = 42\n')

def test_drift_fails_without_writing(tmp_path):
    target = tmp_path / patcher.TARGET
    target.parent.mkdir(parents=True)
    source = PRISTINE.replace('return bool(', 'return not bool(')
    target.write_text(source)
    result = subprocess.run([sys.executable, str(PATCH), '--apply', '--agent-dir', str(tmp_path)],
                            text=True, capture_output=True)
    assert result.returncode == 2
    assert target.read_text() == source

def test_cli_check_apply_and_drift(tmp_path):
    target = tmp_path / patcher.TARGET
    target.parent.mkdir(parents=True)
    target.write_text(PRISTINE)
    def run(mode):
        return subprocess.run([sys.executable, str(PATCH), mode, '--agent-dir', str(tmp_path)],
                              capture_output=True, text=True)
    assert run('--check').returncode == 1
    assert run('--apply').returncode == 0
    assert run('--check').returncode == 0
    content = target.read_bytes()
    assert run('--apply').returncode == 0
    assert target.read_bytes() == content
    target.write_text(target.read_text().replace('"暂不"', '"changed"'))
    changed = target.read_bytes()
    assert run('--apply').returncode == 2
    assert target.read_bytes() == changed

def test_real_host_classifier_on_copied_source_when_available():
    host = os.environ.get('HERMES_SOURCE')
    if not host:
        pytest.skip('set HERMES_SOURCE to test the actual host classifier without modifying it')
    source = (Path(host) / patcher.TARGET).read_text()
    import ast
    updated = patcher.patched_source(source)
    tree = ast.parse(updated)
    wanted = {'_TRAILING_CONTINUE_INTENT_RE', '_TRAILING_CONTINUE_INTENT_MAX_CHARS',
              '_TRAILING_CONTINUE_INTENT_ZH_RE', '_chinese_continue_intent', 'trailing_continue_intent'}
    nodes = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name in wanted)
             or (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in wanted for t in n.targets))]
    ns = {'re': re}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<host-classifier>', 'exec'), ns)
    f = ns['trailing_continue_intent']
    assert f('检查报了一项失败。先查清是不是超时引发的。')
    assert f('Found it. Let me now check the logs.')
    assert not f('建议先检查日志。')


@pytest.mark.parametrize('text,attempt,enabled,has_tools,expected', [
    ('检查报了一项失败。先查清是不是超时引发的。', 0, True, True, 'continue'),
    ('检查报了一项失败。先查清是不是超时引发的。', 1, True, True, 'continue'),
    ('检查报了一项失败。先查清是不是超时引发的。', 2, True, True, 'break'),
    ('检查报了一项失败。先查清是不是超时引发的。', 0, False, True, 'break'),
    ('检查报了一项失败。先查清是不是超时引发的。', 0, True, False, 'break'),
    ('检查完成，根因是超时。', 0, True, True, 'break'),
    ('建议先检查日志。', 0, True, True, 'break'),
])
def test_real_host_finalizer_control_flow(tmp_path, text, attempt, enabled, has_tools, expected):
    host_var = os.environ.get('HERMES_SOURCE')
    if not host_var:
        pytest.skip('set HERMES_SOURCE for real host finalizer control flow')
    import json
    import shutil
    host = Path(host_var).resolve()
    overlay = tmp_path / 'overlay'
    shutil.copytree(host / 'agent', overlay / 'agent', ignore=shutil.ignore_patterns('__pycache__'))
    target = overlay / patcher.TARGET
    target.write_text(patcher.patched_source(target.read_text()))
    home = tmp_path / 'home'
    hermes_home = home / '.hermes'
    hermes_home.mkdir(parents=True)
    (hermes_home / 'config.yaml').write_text('{}')
    site_packages = host / 'venv/lib' / ('python%d.%d' % sys.version_info[:2]) / 'site-packages'
    env = dict(os.environ, HOME=str(home), HERMES_HOME=str(hermes_home),
               HERMES_DISABLE_LAZY_INSTALLS='1', HERMES_ENABLE_PROJECT_PLUGINS='0')
    for key in ('HERMES_PROFILE', 'HERMES_SESSION_ID', 'HERMES_SESSION_KEY'):
        env.pop(key, None)
    env['PYTHONPATH'] = os.pathsep.join([str(overlay), str(host), str(site_packages)])
    script = r'''
import json, sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from agent.turn_final_response import finish_text_response
from agent.conversation_loop import _CODEX_ACK_CONTINUATION_NUDGE
text, attempt, enabled, has_tools, expected = json.loads(sys.argv[1])
agent = MagicMock()
agent._stall_guards = enabled
agent.valid_tool_names = ['terminal'] if has_tools else []
agent._intent_ack_continuation = False
agent.api_mode = 'chat_completions'
agent.model = 'test-model'
agent._strip_think_blocks = lambda s: s
agent._has_content_after_think_block = lambda s: bool(s.strip())
agent._interrupt_requested = False
agent.quiet_mode = True
agent._build_assistant_message = lambda msg, reason: {'role':'assistant','content':msg.content,'finish_reason':reason}
messages = [{'role':'user','content':'检查失败并修复'}, {'role':'assistant','tool_calls':[{'id':'t'}]},
            {'role':'tool','content':'1 failed','tool_call_id':'t'}]
with patch('agent.turn_final_response.apply_stop_gates', return_value=SimpleNamespace(
    continue_turn=False, pending_verification_response=None, pending_verification_response_previewed=False)), patch(
    'agent.turn_finalizer.apply_llm_output_transform', side_effect=lambda a,t,**kw: (t,False,None)):
    v = finish_text_response(agent, assistant_message=SimpleNamespace(content=text,tool_calls=[]),
        response=None,finish_reason='stop',messages=messages,api_messages=[],conversation_history=[],
        api_call_count=1,user_message='检查失败并修复',active_system_prompt='test',final_response=None,
        _turn_exit_reason=None,_preflight_compression_blocked=False,codex_ack_continuations=attempt,
        truncated_response_parts=[],length_continue_retries=0,_pending_verification_response=None,
        _pending_verification_response_previewed=False,effective_task_id=None)
assert v.action == expected, (v.action, expected)
if expected == 'continue':
    assert v.final_response is None
    assert v.codex_ack_continuations == attempt + 1
    assert messages[-2]['finish_reason'] == 'incomplete'
    assert messages[-1]['content'] == _CODEX_ACK_CONTINUATION_NUDGE
    agent._emit_interim_assistant_message.assert_called_once()
else:
    assert v.final_response == text
    assert messages[-1]['finish_reason'] == 'stop'
print('real-finalizer-passed')
'''
    result = subprocess.run([sys.executable, '-c', script, json.dumps([text, attempt, enabled, has_tools, expected])],
                            env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'real-finalizer-passed' in result.stdout


def test_unpatched_host_misses_the_observed_chinese_tail():
    namespace = {}
    exec(PRISTINE, namespace)
    text = '检查报了一项失败。先查清是不是超时引发的。'
    assert not namespace['trailing_continue_intent'](text)
    assert detector()(text)


def test_already_patched_host_with_changed_safety_guard_is_rejected():
    updated = patcher.patched_source(PRISTINE)
    drifted = updated.replace('len(t) > _TRAILING_CONTINUE_INTENT_MAX_CHARS', 'False')
    with pytest.raises(ValueError, match='preconditions changed'):
        patcher.patched_source(drifted)
