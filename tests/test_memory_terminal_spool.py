import importlib.util
import json
from pathlib import Path

import pytest

from agentcore.memory.turn_spool import TurnSpool


def spool(root, user='owner'):
    return TurnSpool(root, profile='personal', user_id=user, agent_id='assistant')


def captured(s):
    return s.capture(session_id='s', turn_id='t', user_message='决定使用蓝色标记', assistant_reply='已记录')


@pytest.mark.parametrize('verdict', [{}, {'failed': True}, {'completed': False},
                                  {'completed': True, 'interrupted': True},
                                  {'completed': True, 'failed': True}])
def test_missing_or_failed_host_verdict_never_queues(tmp_path, verdict):
    s = spool(tmp_path)
    captured(s)
    assert s.finalize(session_id='s', turn_id='t', **verdict) == 'discarded'
    sent = []
    s.ingest(lambda **kwargs: sent.append(kwargs))
    assert sent == []


def test_restart_after_capture_waits_for_verdict_then_enqueues_once(tmp_path):
    captured(spool(tmp_path))
    s = spool(tmp_path)
    sent = []
    assert s.ingest(lambda **kw: sent.append(kw))['unconfirmed'] == 1
    assert not sent
    s.finalize(session_id='s', turn_id='t', completed=True)
    def enqueue(**kwargs):
        sent.append(kwargs)
        return {'ok': True}
    assert spool(tmp_path).ingest(enqueue)['enqueued'] == 1
    assert spool(tmp_path).ingest(enqueue)['enqueued'] == 0
    assert len(sent) == 1
    assert sent[0]['status'] == 'completed' and sent[0]['user_id'] == 'owner'
    assert captured(s) == 'enqueued'
    assert s.finalize(session_id='s', turn_id='t', completed=True) == 'enqueued'


def test_admission_failure_retries_same_identity(tmp_path):
    s = spool(tmp_path)
    captured(s)
    s.finalize(session_id='s', turn_id='t', completed=True)
    with pytest.raises(RuntimeError, match='turn_enqueue_failed'):
        s.ingest(lambda **kw: {'ok': False})
    keys = []
    s.ingest(lambda **kw: keys.append((kw['session_id'], kw['turn_id'])) or {'ok': True})
    assert keys == [('s', 't')]


def test_cron_capture_is_not_a_human_memory(tmp_path):
    s = spool(tmp_path)
    assert s.capture(session_id='s', turn_id='s:cron:job:run',
                     user_message='定时维护指令', assistant_reply='完成') == 'discarded'
    assert not list(tmp_path.glob('*.json'))


def test_old_cron_spool_and_already_queued_event_are_skipped(tmp_path, monkeypatch):
    from agentcore.memory import runtime_service
    s = spool(tmp_path)
    record = dict(s.identity, session_id='s', turn_id='s:cron:job:run',
                  user_message='定时维护指令', assistant_reply='完成',
                  state='completed', occurred_at=1)
    path = s._path('s', record['turn_id'])
    s._save(path, record)
    def forbidden(*args, **kwargs):
        pytest.fail('automated input reached extraction or admission')
    assert s.ingest(forbidden)['discarded'] == 1
    assert json.loads(path.read_text())['discard_reason'] == 'automated_turn'
    monkeypatch.setattr(runtime_service, 'run_and_flush', forbidden)
    process = runtime_service.scoped_runner(**s.identity)
    result = process(dict(record, status='completed', idempotency_key='k'))
    assert result['ok'] and result['written'] == 0 and result['skipped']


def test_conflicts_and_cross_principal_rejected(tmp_path):
    s = spool(tmp_path)
    captured(s)
    with pytest.raises(ValueError, match='turn_capture_conflict'):
        s.capture(session_id='s', turn_id='t', user_message='different', assistant_reply='')
    s.finalize(session_id='s', turn_id='t', failed=True)
    with pytest.raises(ValueError, match='turn_verdict_conflict'):
        s.finalize(session_id='s', turn_id='t', completed=True)
    bad = tmp_path / 'bad.json'
    bad.write_text('{not-json')
    result = spool(tmp_path, user='other').ingest(lambda **kw: {'ok': True})
    assert result['quarantined'] == 2
    assert {p.name for p in tmp_path.glob('*.json.quarantine')} >= {
        bad.name + '.quarantine', next(tmp_path.glob('*.json.quarantine')).name}


def test_real_plugin_uses_terminal_hook_without_engine_global_mutation(tmp_path):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('memory_bridge_under_test', root / 'plugins/memory_bridge/__init__.py')
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    class Context:
        profile_name = 'personal'
        hooks = {}
        settings = dict(enabled=True, engine_root=str(root), spool_dir=str(tmp_path),
                        user_id='owner', agent_id='assistant')
        def get_config(self, key, default=None):
            return self.settings.get(key, default)
        def register_hook(self, name, fn):
            self.hooks[name] = fn
    ctx = Context()
    plugin.register(ctx)
    assert set(ctx.hooks) == {'post_llm_call', 'on_session_end'}
    ctx.hooks['post_llm_call'](session_id='s', turn_id='t', user_message='决定保留证据', assistant_response='好的')
    record = json.loads(next(tmp_path.glob('*.json')).read_text())
    assert record['state'] == 'awaiting_verdict'
    ctx.hooks['on_session_end'](session_id='s', turn_id='t', completed=True, failed=False, interrupted=False)
    assert json.loads(next(tmp_path.glob('*.json')).read_text())['state'] == 'completed'
