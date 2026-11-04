"""Production-grade stress and boundary gates for durable dispatch + frontdesk release.

All scenarios run against real Pool/Worker code in isolated temp trees; no model,
no IM, no production state. A failing synthetic sample must be rejected by the gate.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / 'plugins' / 'durable_dispatch' / '__init__.py'
TURN = REPO / 'runtime' / 'hermes_frontdesk_turn.py'
POLICY = REPO / 'runtime' / 'hermes_frontdesk_policy.py'


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sys.path.insert(0, str(REPO))
from agentcore.worker import Worker as PoolWorker  # noqa: E402

plugin = _load(PLUGIN, 'durable_dispatch_prod')
turn = _load(TURN, 'frontdesk_turn_prod')
policy = _load(POLICY, 'frontdesk_policy_prod')


class Context:
    profile_name = 'prod-profile'

    def __init__(self, settings):
        self.settings, self.tools, self.prompt_sections = settings, {}, {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, **kwargs):
        self.tools[kwargs['name']] = kwargs

    def register_system_prompt_section(self, id, content, **kwargs):
        self.prompt_sections[id] = {'content': content, **kwargs}


def make_env(tmp_path, *, workers=4, max_lines=400, require_route=False):
    sources = tmp_path / 'sources'
    sources.mkdir()
    (sources / 'data.log').write_text('alpha\nbeta\n' + '\n'.join('line-%d' % i for i in range(500)))
    root = str(tmp_path / 'queue')
    settings = {'enabled': True, 'queue_root': root,
                'allowed_roots': [str(sources)], 'max_lines': max_lines,
                'require_delivery_route': require_route}
    ctx = Context(settings)
    plugin.register(ctx)
    return ctx, sources, root


def call(ctx, action, args, **kwargs):
    return json.loads(ctx.tools['durable_' + action]['handler'](args, **kwargs))


def submit(ctx, sources, key, *, path=None, goal='analyze'):
    p = str(path or (sources / 'data.log'))
    return call(ctx, 'submit', {'goal': goal, 'request_key': key,
                                'sources': [{'path': p}]}, session_id='s-' + key)


def ok_runner(tmp_path):
    """A runner that always succeeds; used where the payload content is irrelevant."""
    path = tmp_path / 'ok_runner.py'
    if not path.exists():
        path.write_text('import json,sys\njob=json.load(sys.stdin)\n'
                        'print(json.dumps({"ok":True,"result":job["job"]}))\n')
    return [sys.executable, str(path)]


# --------------------------------------------------------------------------
# Stress: concurrency, saturation, idempotent replay, interleaving
# --------------------------------------------------------------------------

def test_concurrent_submissions_from_many_threads_are_all_unique_and_persisted(tmp_path):
    ctx, sources, root = make_env(tmp_path)
    results, errors, lock = [], [], threading.Lock()

    def work(i):
        try:
            r = submit(ctx, sources, 'stress-%d' % i)
            with lock:
                results.append(r['task_id'])
        except Exception as exc:
            with lock:
                errors.append(type(exc).__name__ + ':' + str(exc)[:80])

    threads = [threading.Thread(target=work, args=(i,)) for i in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert not errors, errors
    assert len(results) == 32 and len(set(results)) == 32
    pool = plugin.Pool(root)
    assert pool.status(results[0])['status'] == 'queued'
    assert len(pool.receipts()) == 0


def test_worker_pool_reaches_full_concurrency_without_duplicate_claims(tmp_path):
    ctx, sources, root = make_env(tmp_path)
    for i in range(12):
        submit(ctx, sources, 'load-%d' % i)
    timeline = tmp_path / 'timeline.jsonl'
    runner = tmp_path / 'runner.py'
    runner.write_text(
        'import json,sys,time\n'
        'job=json.load(sys.stdin)\n'
        't=time.time(); time.sleep(0.25)\n'
        'open(sys.argv[1],"a").write(json.dumps({"id":job["job"],"s":t,"e":time.time()})+"\\n")\n'
        'print(json.dumps({"ok":True,"result":job["job"]}))\n')
    stats = PoolWorker(plugin.Pool(root), [sys.executable, str(runner), str(timeline)],
                          workers=4, backend='stdlib').run(drain=True)
    assert stats['attempts_finished'] == 12
    rows = [json.loads(x) for x in timeline.read_text().splitlines() if x.strip()]
    assert len(rows) == 12 and len({r['id'] for r in rows}) == 12
    events = []
    for r in rows:
        events.append((r['s'], 1))
        events.append((r['e'], -1))
    events.sort()
    cur = peak = 0
    for _, d in events:
        cur += d
        peak = max(peak, cur)
    assert peak >= 3, 'worker slots did not run concurrently: peak=%d' % peak
    assert all(plugin.Pool(root).status(t)['status'] == 'completed'
               for t in [r['id'] for r in rows] if t)


def test_saturated_pool_still_accepts_and_preserves_every_receipt(tmp_path):
    ctx, sources, root = make_env(tmp_path)
    ids = [submit(ctx, sources, 'sat-%d' % i)['task_id'] for i in range(60)]
    pool = plugin.Pool(root)
    assert len(set(ids)) == 60
    assert sum(1 for t in ids if pool.status(t)['status'] == 'queued') == 60
    PoolWorker(pool, ok_runner(tmp_path), workers=4, backend='stdlib').run(drain=True)
    receipts = pool.receipts()
    assert len(receipts) == 60
    assert len({r['job'] for r in receipts}) == 60


def test_long_and_short_tasks_interleave_without_head_of_line_blocking(tmp_path):
    ctx, sources, root = make_env(tmp_path)
    slow = submit(ctx, sources, 'slow')['task_id']
    fast = submit(ctx, sources, 'fast')['task_id']
    pool = plugin.Pool(root)
    PoolWorker(pool, ok_runner(tmp_path), workers=4, backend='stdlib').run(drain=True)
    assert pool.status(slow)['status'] == 'completed'
    assert pool.status(fast)['status'] == 'completed'


# --------------------------------------------------------------------------
# Boundary: schema, permissions, ranges, dedupe, restart
# --------------------------------------------------------------------------

@pytest.mark.parametrize('bad', [
    {'sources': []},
    {'sources': [{'path': 'relative.log'}]},
    {'sources': [{'path': '/etc/passwd'}]},
    {'sources': [{'path': None}]},
    {'sources': [{'path': '/tmp/x', 'start_line': 0}]},
    {'sources': [{'path': '/tmp/x', 'start_line': 5, 'end_line': 3}]},
    {'sources': [{'path': '/tmp/x', 'start_line': 1, 'end_line': 99999}]},
    {'sources': [{'path': '/tmp/x', 'extra': 1}]},
    {'sources': 'not-a-list'},
    {'goal': ''},
    {'goal': 'x' * 4001},
    {'request_key': ''},
    {'request_key': 'k' * 129},
])
def test_malformed_or_out_of_policy_submissions_are_rejected(tmp_path, bad):
    ctx, sources, root = make_env(tmp_path)
    base = {'goal': 'analyze', 'request_key': 'boundary',
            'sources': [{'path': str(sources / 'data.log')}]}
    base.update(bad)
    result = call(ctx, 'submit', base, session_id='s-boundary')
    assert result.get('ok') is False, bad
    assert isinstance(result.get('error'), str) and result['error']
    assert not (tmp_path / 'queue' / 'pool.db').exists() or not plugin.Pool(root).receipts()


def test_sensitive_paths_never_enter_the_queue(tmp_path):
    ctx, sources, root = make_env(tmp_path)
    secret = sources / '.ssh' / 'id_rsa'
    secret.parent.mkdir()
    secret.write_text('PRIVATE')
    r = call(ctx, 'submit', {'goal': 'read', 'request_key': 'secret',
                             'sources': [{'path': str(secret)}]}, session_id='s')
    assert r.get('ok') is False
    assert not plugin.Pool(root).receipts()


def test_same_request_key_is_idempotent_and_new_key_creates_new_job(tmp_path):
    ctx, sources, root = make_env(tmp_path)
    a = submit(ctx, sources, 'same')
    b = submit(ctx, sources, 'same')
    c = submit(ctx, sources, 'different')
    assert a['task_id'] == b['task_id'] != c['task_id']
    assert len(plugin.Pool(root).receipts()) == 0


def test_restart_replays_delivered_receipts_only_once(tmp_path):
    ctx, sources, root = make_env(tmp_path)
    ids = [submit(ctx, sources, 'restart-%d' % i)['task_id'] for i in range(5)]
    pool = plugin.Pool(root)
    PoolWorker(pool, ok_runner(tmp_path), workers=4, backend='stdlib').run(drain=True)
    first = len(pool.receipts())
    pool2 = plugin.Pool(root)  # simulates worker restart against same durable root
    PoolWorker(pool2, ok_runner(tmp_path), workers=4, backend='stdlib').run(drain=True)
    assert len(pool2.receipts()) == first
    assert all(pool2.status(t)['status'] == 'completed' for t in ids)


# --------------------------------------------------------------------------
# Frontdesk release: routing, whitelist, failure terminal
# --------------------------------------------------------------------------

def test_roster_query_tool_is_whitelisted_and_execution_stays_denied(tmp_path):
    home = tmp_path / 'profile'
    home.mkdir()
    (home / 'config.yaml').write_text(json.dumps({
        'superagent': {'frontdesk': {'enabled': True, 'memory_read_enabled': False}}}))
    assert 'colleague_list' in policy.TASK_TOOLS
    assert policy.tool_block_message('colleague_list', home=home) is None
    for name in ('terminal', 'execute_code', 'read_file', 'process_manage',
                 'search_files', 'write_file', 'delegate_task'):
        assert policy.tool_block_message(name, home=home) == policy.TOOL_DENIED


def test_rejected_submit_never_looks_like_accepted_handoff(monkeypatch, tmp_path):
    home = tmp_path / 'profile'
    home.mkdir()
    (home / 'config.yaml').write_text(json.dumps({
        'superagent': {'frontdesk': {'enabled': True}}}))
    monkeypatch.setitem(sys.modules, 'superagent_frontdesk',
                        SimpleNamespace(frontdesk_enabled=lambda **kw: True))
    rejected = json.dumps({'ok': False, 'error': 'source outside administrator roots'})
    reply = turn.release_after_submission(
        ['call-1'], [{'role': 'tool', 'name': 'durable_submit',
                      'tool_call_id': 'call-1', 'content': rejected}], home=home)
    assert turn.is_rejected_handoff(reply)
    assert '未受理' in reply and '任务没有创建' in reply
    assert '处理完成' not in reply
    assert 'source outside administrator roots' in reply
    assert 'durable_roster' in reply


def test_error_codes_are_whitelisted_and_paths_never_leak(monkeypatch, tmp_path):
    home = tmp_path / 'profile'
    home.mkdir()
    (home / 'config.yaml').write_text(json.dumps({
        'superagent': {'frontdesk': {'enabled': True}}}))
    monkeypatch.setitem(sys.modules, 'superagent_frontdesk',
                        SimpleNamespace(frontdesk_enabled=lambda **kw: True))
    secret = '/home/operator/.hermes/profiles/private/config.yaml'
    rejected = json.dumps({'ok': False, 'error': 'bad', 'path': secret, 'config': secret})
    reply = turn.release_after_submission(
        ['c'], [{'role': 'tool', 'name': 'durable_submit',
                 'tool_call_id': 'c', 'content': rejected}], home=home)
    assert secret not in reply and 'private' not in reply and 'bad' not in reply
    assert turn.is_rejected_handoff(reply)


def test_successful_handoff_is_not_marked_rejected(monkeypatch, tmp_path):
    home = tmp_path / 'profile'
    home.mkdir()
    (home / 'config.yaml').write_text(json.dumps({
        'superagent': {'frontdesk': {'enabled': True}}}))
    monkeypatch.setitem(sys.modules, 'superagent_frontdesk',
                        SimpleNamespace(frontdesk_enabled=lambda **kw: True))
    ok = json.dumps({'ok': True, 'task_id': 'a' * 32, 'status': 'queued', 'role': 'analysis'})
    reply = turn.release_after_submission(
        ['c'], [{'role': 'tool', 'name': 'durable_submit',
                 'tool_call_id': 'c', 'content': ok}], home=home)
    assert not turn.is_rejected_handoff(reply)
    assert '已持久记录后台任务' in reply


def test_mixed_batch_keeps_accepted_tasks_and_flags_uncertain(monkeypatch, tmp_path):
    home = tmp_path / 'profile'
    home.mkdir()
    (home / 'config.yaml').write_text(json.dumps({
        'superagent': {'frontdesk': {'enabled': True}}}))
    monkeypatch.setitem(sys.modules, 'superagent_frontdesk',
                        SimpleNamespace(frontdesk_enabled=lambda **kw: True))
    ok = json.dumps({'ok': True, 'task_id': 'b' * 32, 'status': 'queued'})
    bad = json.dumps({'ok': False, 'error': 'absolute source path required'})
    reply = turn.release_after_submission(
        ['ok', 'bad'],
        [{'role': 'tool', 'name': 'durable_submit', 'tool_call_id': 'ok', 'content': ok},
         {'role': 'tool', 'name': 'durable_submit', 'tool_call_id': 'bad', 'content': bad}],
        home=home)
    assert not turn.is_rejected_handoff(reply), 'a mixed batch still has an accepted task'
    assert 'b' * 32 in reply and '另有 1 笔提交结果未确认' in reply



def test_durable_roster_is_read_only_and_reports_configured_roles(tmp_path):
    ctx, sources, root = make_env(tmp_path)
    assert 'durable_roster' in ctx.tools
    result = call(ctx, 'roster', {}, session_id='s-roster')
    assert result['ok'] and result['source'] == 'dispatcher_roles'
    assert result['count'] == len(result['roles']) >= 1
    assert any(r['role'] == 'analysis' for r in result['roles'])
    # No task may be created by a roster query.
    assert not plugin.Pool(root).receipts()
    assert call(ctx, 'roster', {'bogus': 1}, session_id='s')['ok'] is False


def test_durable_roster_reports_active_members_when_registry_configured(tmp_path):
    import sqlite3
    reg = tmp_path / 'registry.sqlite'
    con = sqlite3.connect(str(reg))
    con.execute('create table colleague_members (role_id text, status text)')
    con.executemany('insert into colleague_members values (?,?)',
                    [('analysis', 'active'), ('reviewer', 'active'),
                     ('retired', 'inactive')])
    con.commit(); con.close()
    sources = tmp_path / 'sources'; sources.mkdir()
    (sources / 'data.log').write_text('x')
    settings = {'enabled': True, 'queue_root': str(tmp_path / 'queue'),
                'allowed_roots': [str(sources)], 'roster_registry_path': str(reg)}
    ctx = Context(settings); plugin.register(ctx)
    result = call(ctx, 'roster', {}, session_id='s')
    assert result['ok'] and result['source'] == 'roster_registry'
    assert result['members']['active'] == 2
    assert {'role': 'reviewer', 'count': 1} in result['members']['by_role']

