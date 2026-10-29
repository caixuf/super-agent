"""录制器必须产出「审计器会接受」的 capture —— 否则导出器等于没有。

这些用例用假单调时钟造一份三场景完整轨迹，通过 Recorder 录，再交给
``acceptance_evidence.evaluate`` 判。手工拼 JSON 也能通过审计器，所以单靠审计器
的用例证明不了导出侧；这里证明的是**录制 API 的形状与审计契约一致**。
"""
from __future__ import annotations

import json

import pytest

from agentcore.acceptance_capture import Recorder, seal_from_file
from agentcore.acceptance_evidence import evaluate


class Clock:
    """确定性单调时钟：只往前走，测试里手推。"""

    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


TASK = 'a' * 32


def _burst(rec: Recorder, clock: Clock):
    session, route = 'sess-burst', 'route-burst'
    for index, (mid, moment, answer) in enumerate(
            [('m1', 10.0, '108'), ('m2', 11.0, '210'), ('m3', 12.0, '312')], start=1):
        clock.now = moment
        rec.expect(mid, scenario='same_session_burst', max_reply_seconds=30, expected_text=answer)
        rec.record('message_in', 'in-' + mid, message_id=mid, session_id=session,
                   route_key=route, origin='user', content_hash='h-' + mid)
    for index, mid in enumerate(['m1', 'm2', 'm3'], start=1):
        start, end = 13.0 + (index - 1) * 8, 20.0 + (index - 1) * 8
        clock.now = start
        rec.record('turn_start', 'ts-' + mid, turn_id='t-' + mid, session_id=session,
                   route_key=route, origin='user', lane='main', message_ids=[mid])
        clock.now = end
        rec.record('turn_end', 'te-' + mid, turn_id='t-' + mid, session_id=session, route_key=route)


def _cross(rec: Recorder, clock: Clock):
    windows = [('m4', 'sess-a', 'route-a', 40.0, 42.0, 60.0, 'a4'),
               ('m5', 'sess-b', 'route-b', 41.0, 43.0, 55.0, 'b5')]
    for mid, session, route, inbound, start, end, answer in windows:
        clock.now = inbound
        rec.expect(mid, scenario='cross_session', max_reply_seconds=30, expected_text=answer)
        rec.record('message_in', 'in-' + mid, message_id=mid, session_id=session,
                   route_key=route, origin='user', content_hash='h-' + mid)
        clock.now = start
        rec.record('turn_start', 'ts-' + mid, turn_id='t-' + mid, session_id=session,
                   route_key=route, origin='user', lane='main', message_ids=[mid])
        clock.now = end
        rec.record('turn_end', 'te-' + mid, turn_id='t-' + mid, session_id=session, route_key=route)


def _background(rec: Recorder, clock: Clock):
    session, route = 'sess-bg', 'route-bg'
    clock.now = 70.0
    rec.expect('m6', scenario='background_interjection', max_reply_seconds=60,
               expected_text='submitted', kind='analysis')
    rec.record('message_in', 'in-m6', message_id='m6', session_id=session,
               route_key=route, origin='user', content_hash='h-m6')
    clock.now = 71.0
    rec.record('turn_start', 'ts-m6', turn_id='t-m6', session_id=session, route_key=route,
               origin='user', lane='main', message_ids=['m6'])
    clock.now = 72.0
    rec.record('tool_call', 'call-m6', call_id='call-1', turn_id='t-m6',
               tool_name='durable_submit', session_id=session, route_key=route)
    clock.now = 73.0
    rec.record('tool_result', 'res-m6', call_id='call-1', tool_name='durable_submit', ok=True,
               task_id=TASK, session_id=session, route_key=route, turn_id='t-m6')
    clock.now = 74.0
    rec.record('message_out', 'out-m6', reply_to='m6', final=True, delivery_status='confirmed',
               transport_receipt='rc-1', session_id=session, route_key=route, turn_id='t-m6',
               text='submitted')
    clock.now = 75.0
    rec.record('turn_end', 'te-m6', turn_id='t-m6', session_id=session, route_key=route)

    # 插话：入站在任务 running 期间到达，回复也在同一 attempt 内发出。
    clock.now = 80.0
    rec.expect('m7', scenario='background_interjection', max_reply_seconds=30,
               expected_text='43', while_task_from='m6')
    rec.record('message_in', 'in-m7', message_id='m7', session_id=session,
               route_key=route, origin='user', content_hash='h-m7')
    rec.record('task_probe', 'probe-arrival', task_id=TASK, status='running', attempt='att-1',
               owner_session_id=session, route_key=route, position='arrival', message_id='m7',
               reply_event_id='out-m7', at=79.8)
    clock.now = 81.0
    rec.record('turn_start', 'ts-m7', turn_id='t-m7', session_id=session, route_key=route,
               origin='user', lane='main', message_ids=['m7'])
    rec.record('task_transition', 'tr-1', task_id=TASK, status='running', at=85.0)
    rec.record('task_probe', 'probe-before', task_id=TASK, status='running', attempt='att-1',
               owner_session_id=session, route_key=route, position='before',
               reply_event_id='out-m7', at=94.5)
    clock.now = 95.0
    rec.record('message_out', 'out-m7', reply_to='m7', final=True, delivery_status='confirmed',
               transport_receipt='rc-2', session_id=session, route_key=route, turn_id='t-m7',
               text='43')
    rec.record('task_probe', 'probe-after', task_id=TASK, status='running', attempt='att-1',
               owner_session_id=session, route_key=route, position='after',
               reply_event_id='out-m7', at=95.4)
    clock.now = 95.5
    rec.record('turn_end', 'te-m7', turn_id='t-m7', session_id=session, route_key=route)


def _answers(rec: Recorder, clock: Clock):
    replies = [('m1', 'sess-burst', 'route-burst', 't-m1', 20.5, '108'),
               ('m2', 'sess-burst', 'route-burst', 't-m2', 28.5, '210'),
               ('m3', 'sess-burst', 'route-burst', 't-m3', 36.5, '312'),
               ('m4', 'sess-a', 'route-a', 't-m4', 60.5, 'a4'),
               ('m5', 'sess-b', 'route-b', 't-m5', 55.5, 'b5')]
    for mid, session, route, turn, moment, answer in replies:
        clock.now = moment
        rec.record('message_out', 'out-' + mid, reply_to=mid, final=True,
                   delivery_status='confirmed', transport_receipt='rc-' + mid,
                   session_id=session, route_key=route, turn_id=turn, text=answer)


def _full_capture(clock: Clock, path=None) -> Recorder:
    rec = Recorder(clock=clock, path=path)
    _burst(rec, clock)
    _cross(rec, clock)
    _answers(rec, clock)
    _background(rec, clock)
    return rec


def test_recorded_three_scenario_trace_passes_the_auditor():
    clock = Clock()
    record = _full_capture(clock).seal(close_at=200.0)
    verdict = evaluate(record)
    assert verdict['passed'] is True, verdict['failed_gates']
    assert verdict['durable_task_ids'] == [TASK]
    assert verdict['logical_inbound_count'] == 7
    assert record['capture']['complete'] is True
    assert record['capture']['clock'] == 'host_monotonic'


def test_sealed_file_round_trips_to_the_same_record(tmp_path):
    path = tmp_path / 'capture.ndjson'
    clock = Clock()
    sealed = _full_capture(clock, path=path).seal(close_at=200.0)
    reloaded = seal_from_file(path)
    assert reloaded == sealed
    assert evaluate(reloaded)['passed'] is True


def test_unsealed_file_reports_incomplete_instead_of_passing(tmp_path):
    path = tmp_path / 'crashed.ndjson'
    clock = Clock()
    _full_capture(clock, path=path)  # 进程在此崩溃：没有 __sealed 行
    record = seal_from_file(path)
    assert record['capture']['complete'] is False
    assert 'incomplete_capture' in evaluate(record)['failed_gates']


def test_declared_gap_fails_the_capture_instead_of_being_silently_dropped():
    clock = Clock()
    rec = _full_capture(clock)
    rec.gap('gateway log rotated mid-window')
    record = rec.seal(close_at=200.0)
    assert record['capture']['complete'] is False
    assert 'incomplete_capture' in evaluate(record)['failed_gates']


def test_recorder_refuses_mislabelled_or_out_of_window_events():
    clock = Clock()
    rec = Recorder(clock=clock)
    with pytest.raises(ValueError):
        rec.record('not_an_event', 'x-1')
    with pytest.raises(ValueError):
        rec.record('message_in', 'x-2', source='transport', message_id='m1', session_id='s',
                   route_key='r', origin='user', content_hash='h')
    with pytest.raises(ValueError):
        rec.record('message_in', 'x-3', at=-1.0, message_id='m1', session_id='s', route_key='r',
                   origin='user', content_hash='h')
    with pytest.raises(ValueError):
        rec.record('message_in', 'x-4', at=float('nan'), message_id='m1', session_id='s',
                   route_key='r', origin='user', content_hash='h')
    rec.record('message_in', 'x-5', message_id='m1', session_id='s', route_key='r',
               origin='user', content_hash='h')
    with pytest.raises(ValueError):
        rec.record('message_in', 'x-5', message_id='m2', session_id='s', route_key='r',
                   origin='user', content_hash='h')


def test_sealed_recorder_refuses_further_evidence():
    clock = Clock()
    rec = _full_capture(clock)
    rec.seal(close_at=200.0)
    with pytest.raises(RuntimeError):
        rec.record('message_in', 'late', message_id='m9', session_id='s', route_key='r',
                   origin='user', content_hash='h')
    with pytest.raises(RuntimeError):
        rec.expect('m9', scenario='cross_session', max_reply_seconds=5)
    with pytest.raises(RuntimeError):
        rec.gap('late')
    with pytest.raises(RuntimeError):
        rec.seal()


def test_expectation_budget_and_scenario_are_mandatory():
    rec = Recorder(clock=Clock())
    with pytest.raises(ValueError):
        rec.expect('m1', scenario='same_session_burst', max_reply_seconds=0)
    with pytest.raises(ValueError):
        rec.expect('m1', scenario='', max_reply_seconds=10)
    rec.expect('m1', scenario='cross_session', max_reply_seconds=10)
    with pytest.raises(ValueError):
        rec.expect('m1', scenario='cross_session', max_reply_seconds=10)


def test_missing_expectation_is_caught_by_the_auditor_not_the_recorder():
    """录制器不替人做判定：漏写预期时，审计器必须报 request_coverage。"""
    clock = Clock()
    rec = _full_capture(clock)
    record = rec.seal(close_at=200.0)
    record['requests'].pop('m7')
    assert 'request_coverage' in evaluate(record)['failed_gates']


def test_capture_is_json_serializable(tmp_path):
    clock = Clock()
    record = _full_capture(clock).seal(close_at=200.0)
    json.dumps(record)  # 不快照化就没法交给审计 CLI/归档
    assert evaluate(json.loads(json.dumps(record)))['passed'] is True
