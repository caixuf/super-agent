"""The submitting user's own words travel with a dispatched task.

Live failure: frontdesk briefs dropped the user's wording, referenced IDs and the
earlier turns, so backend colleagues received tasks they could not complete.
"""
import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('dd_requester_ctx', REPO / 'plugins' / 'durable_dispatch' / '__init__.py')
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
rspec = importlib.util.spec_from_file_location('runner_requester_ctx', REPO / 'install' / 'worker' / 'dev_exec_runner.py')
runner = importlib.util.module_from_spec(rspec)
rspec.loader.exec_module(runner)


class Context:
    profile_name = 'p'

    def __init__(self, settings):
        self.settings, self.tools, self.prompt_sections, self.hooks = settings, {}, {}, {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, **kwargs):
        self.tools[kwargs['name']] = kwargs

    def register_system_prompt_section(self, id, content, **kwargs):
        self.prompt_sections[id] = {'content': content, **kwargs}

    def register_hook(self, name, fn):
        self.hooks[name] = fn


def _payloads(queue_root):
    with plugin.Pool(queue_root).db() as db:
        return [json.loads(r[0]) for r in db.execute('SELECT payload FROM jobs').fetchall()]


def _dev(tmp_path):
    return plugin.DispatchService(str(tmp_path / 'q'), 'p', [str(tmp_path)], role='dev')


def test_frontdesk_registers_capture_hook(tmp_path):
    ctx = Context({'enabled': True, 'queue_root': str(tmp_path / 'q'), 'allowed_roots': [str(tmp_path)]})
    plugin.register(ctx)
    ctx.hooks['pre_llm_call'](session_id='hook-s', user_message='原话', conversation_history=[])
    assert plugin.recall_request_context('hook-s')['user_request'] == '原话'


def test_submit_carries_user_request_and_conversation(tmp_path):
    history = [
        {'role': 'user', 'content': '帮我看下 story 7148333175 的用例'},
        {'role': 'assistant', 'content': '这条需求在 V1103 版本，目前未覆盖。'},
        {'role': 'tool', 'content': 'raw tool output must not leak'},
        {'role': 'user', 'content': [{'type': 'text', 'text': '那就按这个补一条反例，跑一下'}]},
    ]
    plugin.remember_request_context('s1', '那就按这个补一条反例，跑一下', history)
    out = _dev(tmp_path).submit('s1', {'goal': '补反例并跑测', 'request_key': 'k1'})
    assert out['ok']
    [payload] = _payloads(str(tmp_path / 'q'))
    assert payload['_user_request'] == '那就按这个补一条反例，跑一下'
    conv = payload['_conversation']
    assert '7148333175' in conv and 'V1103' in conv
    assert 'raw tool output' not in conv
    assert '补一条反例' not in conv  # current message is not duplicated


def test_other_session_context_is_not_borrowed(tmp_path):
    plugin.remember_request_context('other', '别人的请求')
    _dev(tmp_path).submit('mine', {'goal': 'task', 'request_key': 'k2'})
    [payload] = _payloads(str(tmp_path / 'q'))
    assert '_user_request' not in payload


def test_context_expires():
    plugin.remember_request_context('s-exp', 'hello', now=1000.0)
    assert plugin.recall_request_context('s-exp', now=1000.0 + 10)['user_request'] == 'hello'
    assert plugin.recall_request_context('s-exp', now=1000.0 + plugin._REQUEST_CONTEXT_TTL + 1) is None


def test_routing_prompt_keeps_domain_experts_and_brief_rules(tmp_path):
    class Svc:
        def __init__(self, role):
            self.display_name = role.upper()
            self.duty = '负责' + role + '相关的专业工作，' * 6
            self.target_host = 'host-a'
    services = {'analysis': Svc('analysis'), 'dev': Svc('dev')}
    services.update({r: Svc(r) for r in ('architect', 'cases', 'reviewer', 'test', 'triage', 'ops', 'replay', 'maintainer')})
    text = plugin._render_briefing_prompt(services)
    assert 'Domain experts' in text
    assert all(('role=%s' % r) in text for r in ('architect', 'maintainer'))
    assert 'Self-contained' in text and 'ask one short question' in text
    assert len(text) <= plugin._SECTION_BUDGET
    assert len(plugin._render_routing_base()) <= plugin._SECTION_BUDGET


def test_runner_composes_user_words_after_brief():
    goal = runner._compose_goal('补反例并跑测', {'_user_request': '那就按这个补一条反例，跑一下',
                                              '_conversation': '用户: story 7148333175'})
    assert goal.startswith('补反例并跑测')
    assert '[用户原话' in goal and '那就按这个补一条反例' in goal
    assert '7148333175' in goal
    assert runner._compose_goal('only', {}) == 'only'

