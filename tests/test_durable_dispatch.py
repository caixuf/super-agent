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
        self.settings, self.tools = settings, {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, **kwargs):
        self.tools[kwargs['name']] = kwargs


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
    result = call(context, 'submit', args, session_id='trusted-session')
    assert result['ok'] and result['status'] == 'queued'
    assert 'finish this turn' in result['next_action']
    assert call(context, 'submit', args, session_id='trusted-session')['task_id'] == result['task_id']
    with plugin.Pool(context.settings['queue_root']).db() as db:
        rows = db.execute('SELECT payload FROM jobs').fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]['payload'])
    assert 'trusted-session' not in rows[0]['payload']
    assert set(payload) == {'goal', 'sources', '_dispatch_owner'}
    assert payload['sources'] == [{'path': args['sources'][0]['path'], 'start_line': 1}]
    assert 'end_line' not in payload['sources'][0], 'runner must choose the actual end of a short file'


def test_missing_identity_and_model_spoofing_are_denied(tmp_path, monkeypatch):
    context, args = make_context(tmp_path)
    monkeypatch.setenv('HERMES_SESSION_ID', 'environment-is-not-dispatch-proof')
    assert not call(context, 'submit', args)['ok']
    for key in ('session_id', 'owner', '_dispatch_owner', 'runner', 'command'):
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
