import os

import pytest

from agentcore.colleague_queue_delivery import SignedQueueDelivery
from agentcore.colleague_readonly_runtime import ReadOnlyColleagueRuntime
from agentcore.colleague_registry import Actor, Registry, Supervisor
from agentcore.completion_backfill import drain
from agentcore.direct_completion import DirectCompletionTransport
from agentcore.pool import Pool


@pytest.fixture
def setting(tmp_path):
    registry = Registry(tmp_path / 'registry.sqlite', profile_id='team', primary_id='leader')
    actor, supervisor = Actor('team', 'leader'), Supervisor('team')
    registry.define_role(actor=actor, role_id='developer', label='开发', duty='只读分析',
                         boundaries=['只读'], request_id='role')
    registry.hire(actor=actor, member_id='dev', role_id='developer', display_name='小程', request_id='hire')
    registry.observe_runtime(supervisor=supervisor, member_id='dev', generation=1, state='running')
    source_dir = tmp_path / 'source'
    source_dir.mkdir()
    source = source_dir / 'code.py'
    source.write_text('def f():\n    return 42\n')
    state = tmp_path / 'runtime'
    state.mkdir(mode=0o700)
    owner = {'profile': 'leader', 'session': 'session'}
    allowed = [True]
    def model(messages, **options):
        return {'conclusion': '返回 42', 'claims': [{'statement': '返回 42',
                'evidence': [{'source_id': 'source_1', 'line': 2, 'quote': 'return 42'}]}],
                'limitations': []}
    runtime = ReadOnlyColleagueRuntime(registry, supervisor, state_dir=state,
        policy={'allowed_roots': [str(source_dir)]}, model=model,
        authorize_owner=lambda candidate: allowed[0] and candidate == owner)
    result = runtime.run(owner=owner, request_id='request-1', member_id='dev', generation=1,
                         goal='解释代码', sources=[{'path': str(source)}])
    assert result['state'] == 'completed'
    pool = Pool(tmp_path / 'queue')
    address = {**owner, 'route': 'route', 'channel': 'chat', 'chat': 'chat-1'}
    return runtime, owner, allowed, pool, address


def _queue(pool, address, *, artifacts=()):
    job = pool.submit('queue-key', {'goal': 'analysis'}, origin=address)
    row, fd = pool.claim()
    try:
        attempt = pool.root / 'attempts' / row['attempt']
        attempt.mkdir()
        paths = []
        for name in artifacts:
            path = attempt / name
            path.write_text('checked report')
            paths.append(str(path))
        assert pool.finish(job, row['attempt'], True, {'summary': 'untrusted attribution', 'artifacts': paths})
    finally:
        os.close(fd)
    return job, paths


def _transport(tmp_path, setting, *, sender, resolver=None, artifact_policy=None):
    runtime, owner, _, pool, _ = setting
    # This is a local test double, not an attestation about a real platform.
    sender.LITERAL_TEXT_DELIVERY = True
    direct = DirectCompletionTransport(tmp_path / 'direct', profile=owner['profile'], sender=sender,
        render=lambda message: message['summary'], attempt_root=pool.root / 'attempts',
        attempt_provider=lambda job: pool.status(job).get('attempt'))
    resolver = resolver if resolver is not None else lambda job: {'owner': owner, 'request_id': 'request-1'}
    return SignedQueueDelivery(runtime, resolve_binding=resolver, direct_transport=direct,
        snapshot_path=tmp_path / 'snapshots.sqlite', authorize_artifacts=artifact_policy)


def test_unattested_platform_sender_cannot_enable_signed_delivery(setting, tmp_path):
    runtime, owner, _, pool, _ = setting
    def generic_sender(*args, **kwargs):
        raise AssertionError('must not send')
    direct = DirectCompletionTransport(tmp_path / 'direct', profile=owner['profile'], sender=generic_sender,
        render=lambda message: message['summary'], attempt_root=pool.root / 'attempts',
        attempt_provider=lambda job: pool.status(job).get('attempt'))
    with pytest.raises(ValueError, match='literal_text_transport_required'):
        SignedQueueDelivery(runtime, resolve_binding=lambda job: None, direct_transport=direct,
                            snapshot_path=tmp_path / 'snapshots.sqlite')


def test_existing_outbox_sends_signed_result_and_host_approved_attempt_file(setting, tmp_path):
    _, owner, _, pool, address = setting
    job, paths = _queue(pool, address, artifacts=['report.md'])
    sent = []
    def sender(destination, item, **kwargs):
        sent.append((destination, item, kwargs))
        return {'ok': True, 'message_id': 'receipt-' + item['kind']}
    transport = _transport(tmp_path, setting, sender=sender,
        artifact_policy=lambda candidate, actor, proposed: candidate == job and actor == owner and proposed == paths)
    result = drain(pool, transport, profile=owner['profile'])
    assert result['delivered'] == 1 and pool.delivery_record(job)['acknowledged']
    assert [item[1]['kind'] for item in sent] == ['text', 'file']
    assert '主 Bot 转交' in sent[0][1]['text'] and '小程' in sent[0][1]['text']
    assert 'untrusted attribution' not in sent[0][1]['text']
    assert all(item[0] == address for item in sent)
    assert drain(pool, transport, profile=owner['profile'])['delivered'] == 0
    assert len(sent) == 2


@pytest.mark.parametrize('binding', [None, {'owner': {'profile': 'leader', 'session': 'other'}, 'request_id': 'request-1'},
                                     {'owner': {'profile': 'leader', 'session': 'session'}, 'request_id': 'missing'}])
def test_missing_or_foreign_binding_never_sends_or_acks(setting, tmp_path, binding):
    _, owner, _, pool, address = setting
    job, _ = _queue(pool, address)
    sent = []
    transport = _transport(tmp_path, setting, sender=lambda *args, **kwargs: sent.append(args),
                           resolver=lambda _: binding)
    assert drain(pool, transport, profile=owner['profile'])['delivered'] == 0
    assert not pool.delivery_record(job)['acknowledged'] and not sent


def test_attachments_are_deferred_without_host_approval(setting, tmp_path):
    _, owner, _, pool, address = setting
    job, _ = _queue(pool, address, artifacts=['report.md'])
    sent = []
    transport = _transport(tmp_path, setting, sender=lambda *args, **kwargs: sent.append(args))
    assert drain(pool, transport, profile=owner['profile'])['delivered'] == 0
    assert not pool.delivery_record(job)['acknowledged'] and not sent


def test_wrapped_transport_rejects_artifact_from_runtime_not_queue_attempt(setting, tmp_path):
    runtime, owner, _, pool, address = setting
    job, _ = _queue(pool, address)
    wrong = runtime.root / 'outside.md'
    wrong.write_text('not a queue attempt')
    sent = []
    transport = _transport(tmp_path, setting, sender=lambda *args, **kwargs: sent.append(args),
                           artifact_policy=lambda *_: True)
    with pytest.raises(ValueError, match='artifact outside'):
        transport.deliver(address, {'task_id': job, 'status': 'completed', 'summary': 'ignored',
                                   'artifacts': [str(wrong)]}, idempotency_key=job)
    assert not sent


def test_retry_uses_immutable_signed_snapshot_and_does_not_resend_unknown(setting, tmp_path, monkeypatch):
    _, owner, allowed, pool, address = setting
    job, _ = _queue(pool, address)
    sent = []
    def sender(destination, item, **kwargs):
        sent.append(item['text'])
        return {'ok': False, 'not_sent': True} if len(sent) == 1 else {'ok': True, 'message_id': 'receipt'}
    transport = _transport(tmp_path, setting, sender=sender)
    message = {'task_id': job, 'status': 'completed', 'summary': 'ignored', 'truncated': False}
    assert transport.deliver(address, message, idempotency_key=job)['deferred']
    from agentcore.colleague_readonly_dialogue import ReadOnlyDialogue
    monkeypatch.setattr(ReadOnlyDialogue, 'present', lambda *_: (_ for _ in ()).throw(AssertionError('rerendered')))
    allowed[0] = False
    assert transport.deliver(address, message, idempotency_key=job)['deferred']
    allowed[0] = True
    assert transport.deliver(address, message, idempotency_key=job)['accepted']
    assert len(sent) == 2 and sent[0] == sent[1] and '小程' in sent[0]


def test_ambiguous_sender_timeout_never_auto_resends(setting, tmp_path):
    _, _, _, pool, address = setting
    job, _ = _queue(pool, address)
    calls = []
    def sender(*args, **kwargs):
        calls.append(args)
        raise TimeoutError('unknown')
    transport = _transport(tmp_path, setting, sender=sender)
    message = {'task_id': job, 'status': 'completed', 'summary': 'ignored'}
    assert transport.deliver(address, message, idempotency_key=job)['deferred']
    assert transport.deliver(address, message, idempotency_key=job)['deferred']
    assert len(calls) == 1
