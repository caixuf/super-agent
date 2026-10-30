import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / 'plugins' / 'durable_dispatch' / '__init__.py'
spec = importlib.util.spec_from_file_location('durable_dispatch_test_module', PLUGIN)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


class Context:
    profile_name = 'test-profile'

    def __init__(self, settings):
        self.settings, self.tools, self.prompt_sections = settings, {}, {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, **kwargs):
        self.tools[kwargs['name']] = kwargs

    def register_system_prompt_section(self, id, content, **kwargs):
        self.prompt_sections[id] = {'content': content, **kwargs}


def make_context(tmp_path):
    sources = tmp_path / 'sources'
    sources.mkdir()
    source = sources / 'failure.log'
    source.write_text('line one\nline two\n')
    settings = {'enabled': True, 'queue_root': str(tmp_path / 'queue'), 'allowed_roots': [str(sources)]}
    context = Context(settings)
    plugin.register(context)
    args = {'goal': 'Explain this observed failure', 'request_key': 'request-1',
            'sources': [{'path': str(source)}]}
    return context, args


def call(context, action, args, **kwargs):
    return json.loads(context.tools['durable_' + action]['handler'](args, **kwargs))


def test_register_opt_in_and_bounded_submission(tmp_path):
    off = Context({'queue_root': str(tmp_path / 'off')})
    plugin.register(off)
    assert off.tools == {}
    assert not (tmp_path / 'off').exists()
    context, args = make_context(tmp_path)
    assert set(context.tools) == {'durable_submit', 'durable_status', 'durable_cancel'}
    assert set(context.prompt_sections) == {'durable-dispatch-routing'}
    routing = context.prompt_sections['durable-dispatch-routing']['content']
    assert 'durable_submit' in routing and 'finish the turn' in routing
    assert 'not by keywords' in routing
    result = call(context, 'submit', args, session_id='trusted-session')
    assert result['ok'] and result['status'] == 'queued'
    assert 'finish this turn' in result['next_action']
    assert call(context, 'submit', args, session_id='trusted-session')['task_id'] == result['task_id']
    with plugin.Pool(context.settings['queue_root']).db() as db:
        rows = db.execute('SELECT payload FROM jobs').fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]['payload'])
    assert 'trusted-session' not in rows[0]['payload']
    assert set(payload) == {'goal', 'sources', '_dispatch_owner',
                            '_colleague_role', '_colleague_name', '_colleague_label'}
    assert payload['_colleague_role'] == 'analysis'
    assert payload['_colleague_name'] == '主 Bot 汇总'
    assert payload['_colleague_label'] == '汇总'
    assert payload['sources'] == [{'path': args['sources'][0]['path'], 'start_line': 1}]
    assert 'end_line' not in payload['sources'][0], 'runner must choose the actual end of a short file'


def test_missing_identity_and_model_spoofing_are_denied(tmp_path, monkeypatch):
    context, args = make_context(tmp_path)
    monkeypatch.setenv('HERMES_SESSION_ID', 'environment-is-not-dispatch-proof')
    assert not call(context, 'submit', args)['ok']
    for key in ('session_id', 'owner', '_dispatch_owner', 'runner', 'command', 'engine_root'):
        assert not call(context, 'submit', dict(args, **{key: 'spoof'}), session_id='real')['ok']
    with plugin.Pool(context.settings['queue_root']).db() as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0


def test_cross_session_and_profile_access_are_denied(tmp_path):
    context, args = make_context(tmp_path)
    one = call(context, 'submit', args, session_id='alice')['task_id']
    two = call(context, 'submit', args, session_id='bob')['task_id']
    assert one != two
    assert not call(context, 'status', {'task_id': one}, session_id='bob')['ok']
    assert not call(context, 'cancel', {'task_id': one}, session_id='bob')['ok']
    other = plugin.DispatchService(context.settings['queue_root'], 'other-profile', context.settings['allowed_roots'])
    with pytest.raises(PermissionError):
        other.status('alice', {'task_id': one})
    assert call(context, 'status', {'task_id': one}, session_id='alice')['status'] == 'queued'
    assert call(context, 'cancel', {'task_id': one}, session_id='alice')['cancellation_requested']


@pytest.mark.parametrize('name', ['.env', '.env.local', 'private.pem', 'auth.json', 'credentials.json'])
def test_sensitive_sources_are_denied(tmp_path, name):
    context, args = make_context(tmp_path)
    source = tmp_path / 'sources' / name
    source.write_text('synthetic fixture, not a credential')
    args['sources'] = [{'path': str(source)}]
    assert not call(context, 'submit', args, session_id='session')['ok']


def test_sources_stay_in_policy_and_ranges_are_bounded(tmp_path):
    context, args = make_context(tmp_path)
    outside = tmp_path / 'outside.log'
    outside.write_text('outside')
    symlink = tmp_path / 'sources' / 'escape.log'
    symlink.symlink_to(outside)
    for source in ({'path': str(outside)}, {'path': str(symlink)},
                   {'path': str(tmp_path / 'sources')}, {'path': 'relative.log'},
                   dict(args['sources'][0], start_line=0),
                   dict(args['sources'][0], start_line=1, end_line=99999)):
        assert not call(context, 'submit', dict(args, sources=[source]), session_id='session')['ok']


def test_dedupe_conflict_is_explicit_and_result_is_scoped(tmp_path):
    context, args = make_context(tmp_path)
    result = call(context, 'submit', args, session_id='session')
    assert not call(context, 'submit', dict(args, goal='Different task'), session_id='session')['ok']
    pool = plugin.Pool(context.settings['queue_root'])
    row, fd = pool.claim()
    try:
        pool.finish(row['id'], row['attempt'], True, {'report': 'verified report'})
    finally:
        os.close(fd)
    status = call(context, 'status', {'task_id': result['task_id']}, session_id='session')
    assert status['status'] == 'completed'
    assert status['result'] == {'report': 'verified report'}
    assert 'attempt' not in status


def test_real_hermes_registry_dispatch_identity(tmp_path):
    host = Path(os.environ.get('HERMES_SOURCE', Path.home() / '.hermes' / 'hermes-agent'))
    if not (host / 'tools' / 'registry.py').exists():
        pytest.skip('Hermes source unavailable for real registry test')
    source_dir = tmp_path / 'sources'
    source_dir.mkdir()
    source = source_dir / 'failure.log'
    source.write_text('synthetic failure evidence\n')
    code = '''
import importlib.util,json,sys
sys.path.insert(0,sys.argv[1]); sys.path.insert(0,sys.argv[2])
from tools.registry import ToolRegistry
spec=importlib.util.spec_from_file_location('isolated_durable_plugin',sys.argv[3])
p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
registry=ToolRegistry()
class Context:
    profile_name='isolated-profile'
    def get_config(self,key,default=None):
        return {'enabled':True,'queue_root':sys.argv[4],'allowed_roots':[sys.argv[5]]}.get(key,default)
    def register_tool(self,**kw): registry.register(**kw)
    def register_system_prompt_section(self, *args, **kw): pass
p.register(Context())
args={'goal':'Read evidence','sources':[{'path':sys.argv[6]}],'request_key':'real-registry'}
assert json.loads(registry.dispatch('durable_submit',args))['ok'] is False
answer=json.loads(registry.dispatch('durable_submit',args,session_id='trusted-session',task_id='host-task'))
assert answer['ok'] is True
assert json.loads(registry.dispatch('durable_status',{'task_id':answer['task_id']},session_id='other'))['ok'] is False
assert json.loads(registry.dispatch('durable_status',{'task_id':answer['task_id']},session_id='trusted-session'))['status']=='queued'
print('real registry identity passed')
'''
    env = dict(os.environ, HERMES_HOME=str(tmp_path / 'isolated-home'))
    result = subprocess.run([sys.executable, '-c', code, str(host), str(REPO), str(PLUGIN),
                             str(tmp_path / 'queue'), str(source_dir), str(source)],
                            env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-3000:]
    assert 'real registry identity passed' in result.stdout


def test_pool_fallback_uses_only_admin_engine_root(tmp_path, monkeypatch):
    original = plugin.importlib.import_module
    def unavailable(name, *args, **kwargs):
        if name == 'agentcore.pool':
            error = ModuleNotFoundError('engine unavailable')
            error.name = 'agentcore'
            raise error
        return original(name, *args, **kwargs)
    monkeypatch.setattr(plugin.importlib, 'import_module', unavailable)
    before = list(sys.path)
    with pytest.raises(ValueError, match='engine_root'):
        plugin._pool_type()
    with pytest.raises(ValueError, match='engine_root'):
        plugin._pool_type('relative')
    context, args = make_context_for_fallback(tmp_path, str(REPO))
    plugin.register(context)
    assert call(context, 'submit', args, session_id='trusted')['ok']
    assert sys.path == before


def make_context_for_fallback(tmp_path, engine):
    sources = tmp_path / 'sources'
    sources.mkdir()
    source = sources / 'failure.log'
    source.write_text('failure evidence\n')
    return (Context({'enabled': True, 'queue_root': str(tmp_path / 'queue'),
                     'allowed_roots': [str(sources)], 'engine_root': engine}),
            {'goal': 'Read evidence', 'sources': [{'path': str(source)}], 'request_key': 'fallback'})


def test_chinese_summary_remains_structured_despite_json_escaping(tmp_path):
    context, args = make_context(tmp_path)
    task = call(context, 'submit', args, session_id='session')['task_id']
    pool = plugin.Pool(context.settings['queue_root'])
    row, fd = pool.claim()
    try:
        report = {'ok': True, 'result': {'summary': '原文核对通过。' * 250,
                                        'summary_truncated': False,
                                        'artifacts': ['report.md'], 'limitations': ['仅核对原文引用。']}}
        assert len(json.dumps(report)) > 10000
        pool.finish(task, row['attempt'], True, report)
    finally:
        os.close(fd)
    answer = call(context, 'status', {'task_id': task}, session_id='session')
    assert answer['result'] == report
    assert 'result_preview' not in answer
    assert 'result_truncated' not in answer


def test_large_result_preserves_json_structure_and_marks_summary(tmp_path):
    context, args = make_context(tmp_path)
    task = call(context, 'submit', args, session_id='session')['task_id']
    pool = plugin.Pool(context.settings['queue_root'])
    row, fd = pool.claim()
    try:
        pool.finish(task, row['attempt'], True, {'ok': True, 'result': {
            'summary': '核对结果' * 4000, 'summary_truncated': False,
            'artifacts': ['report.md'], 'limitations': ['限制' * 1000] * 5}})
    finally:
        os.close(fd)
    answer = call(context, 'status', {'task_id': task}, session_id='session')
    report = answer['result']['result']
    assert answer['result_truncated'] is True
    assert report['summary_truncated'] is True
    assert len(report['summary']) <= 2000
    assert report['artifacts'] == ['report.md']
    assert len(report['limitations']) <= 3
    assert all(len(v) <= 300 for v in report['limitations'])
    json.loads(json.dumps(answer, ensure_ascii=False))


@pytest.mark.parametrize('terminal', ['completed', 'failed', 'cancelled'])
def test_cancel_terminal_task_does_not_claim_a_new_request(tmp_path, terminal):
    context, args = make_context(tmp_path)
    task = call(context, 'submit', args, session_id='session')['task_id']
    pool = plugin.Pool(context.settings['queue_root'])
    if terminal == 'cancelled':
        pool.cancel(task)
        assert pool.claim() is None
    else:
        row, fd = pool.claim()
        try:
            # Disable replay so a failed fixture is genuinely terminal.
            with pool.db() as db:
                db.execute('UPDATE jobs SET replay_safe=0 WHERE id=?', (task,))
            pool.finish(task, row['attempt'], terminal == 'completed', {})
        finally:
            os.close(fd)
    before = call(context, 'status', {'task_id': task}, session_id='session')
    answer = call(context, 'cancel', {'task_id': task}, session_id='session')
    assert answer['already_terminal'] is True
    assert answer['request_accepted'] is False
    assert answer['status'] == terminal
    assert answer['cancellation_requested'] == before['cancellation_requested']


def test_real_hermes_runtime_without_engine_package(tmp_path):
    host = Path(os.environ.get('HERMES_SOURCE', Path.home() / '.hermes/hermes-agent'))
    runtime = host / 'venv/bin/python' if (host / 'venv/bin/python').is_file() else host / '.venv/bin/python'
    if not runtime.is_file():
        pytest.skip('Actual isolated host interpreter unavailable')
    code = '''
import importlib.util,json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
assert importlib.util.find_spec('agentcore') is None
from hermes_cli.plugins import PluginContext,PluginManager,PluginManifest
from hermes_cli.plugins_state import save_plugin_setting
from tools.registry import registry
home=Path(sys.argv[4]); home.mkdir()
source=home/'failure.log'; source.write_text('checked evidence\\n')
for key,value in {'enabled':True,'engine_root':sys.argv[2],
                  'queue_root':str(home/'queue'),'allowed_roots':[str(home)]}.items():
    save_plugin_setting('durable-dispatch',(key,),value)
spec=importlib.util.spec_from_file_location('isolated_durable_plugin',sys.argv[3])
p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
manifest=PluginManifest(name='durable-dispatch',version='0.1.0',description='test',author='test',path=Path(sys.argv[3]).parent)
manager=PluginManager(scope_key=str(home)); ctx=PluginContext(manifest,manager)
before=list(sys.path)
p.register(ctx)
assert sys.path==before
assert manager._plugin_tool_names=={'durable_submit','durable_status','durable_cancel'}
assert {registry.get_entry(name,scope=manager.scope_key).toolset for name in manager._plugin_tool_names}=={'durable_dispatch'}
assert set(manager._system_prompt_sections)=={'durable-dispatch-routing'}
args={'goal':'Read the evidence','sources':[{'path':str(source)}],'request_key':'event'}
assert not json.loads(registry.dispatch('durable_submit',args,scope=manager.scope_key))['ok']
answer=json.loads(registry.dispatch('durable_submit',args,scope=manager.scope_key,session_id='trusted'))
assert answer['ok'] and answer['status']=='queued'
assert json.loads(registry.dispatch('durable_status',{'task_id':answer['task_id']},scope=manager.scope_key,session_id='trusted'))['status']=='queued'
assert not json.loads(registry.dispatch('durable_cancel',{'task_id':answer['task_id']},scope=manager.scope_key,session_id='other'))['ok']
assert importlib.util.find_spec('agentcore') is None
print('isolated host runtime dispatch passed')
'''
    env = dict(os.environ, HERMES_HOME=str(tmp_path / 'home'))
    env.pop('PYTHONPATH', None)
    result = subprocess.run([str(runtime), '-c', code, str(host), str(REPO), str(PLUGIN),
                             str(tmp_path / 'home')], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-3000:]
    assert 'isolated host runtime dispatch passed' in result.stdout


def test_submit_persists_submitting_conversation_as_origin(tmp_path):
    context, args = make_context(tmp_path)
    result = call(context, 'submit', args, session_id='session-one')
    pool = plugin.Pool(context.settings['queue_root'])
    assert pool.origin(result['task_id']) == {'profile': 'test-profile', 'session': 'session-one'}
    # The one-way owner hash stays the ownership proof; the origin is the address.
    row = pool.status(result['task_id'])
    assert row['status'] == 'queued'


def test_origin_records_which_session_owns_each_task(tmp_path):
    context, args = make_context(tmp_path)
    mine = call(context, 'submit', args, session_id='session-one')['task_id']
    theirs = call(context, 'submit', dict(args, request_key='request-2'), session_id='session-two')['task_id']
    pool = plugin.Pool(context.settings['queue_root'])
    assert pool.origin(mine)['session'] == 'session-one'
    assert pool.origin(theirs)['session'] == 'session-two'
    # Addressability does not weaken the access check.
    assert not call(context, 'status', {'task_id': mine}, session_id='session-two')['ok']
    assert call(context, 'status', {'task_id': mine}, session_id='session-one')['ok']


def test_status_enriches_with_liveness_ledger(tmp_path):
    from agentcore.liveness import LivenessLedger
    ledger_db = tmp_path / "liveness.db"
    ledger = LivenessLedger(ledger_db)

    context, args = make_context(tmp_path)
    service = plugin.DispatchService(
        context.settings['queue_root'],
        'test-profile',
        context.settings['allowed_roots'],
        liveness_ledger=ledger,
    )
    submit_res = service.submit('session-one', args)
    task_id = submit_res['task_id']

    live_entry = ledger.get(task_id)
    assert live_entry is not None
    assert live_entry['state'] == 'queued'
    assert live_entry['owner'] == 'test-profile:session-one'

    ledger.beat(task_id, phase='analyzing', progress=(10, 100))

    status_res = service.status('session-one', {'task_id': task_id})
    assert status_res['ok'] is True
    assert status_res['task_id'] == task_id
    assert status_res['phase'] == 'analyzing'
    assert status_res['progress'] == {'done': 10, 'total': 100, 'percent': 10.0}
    assert 'elapsed_s' in status_res
    assert 'heartbeat_age_s' in status_res

    cancel_res = service.cancel('session-one', {'task_id': task_id})
    assert cancel_res['ok'] is True
    live_cancelled = ledger.get(task_id)
    assert live_cancelled['state'] == 'cancelled'
