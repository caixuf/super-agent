from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import pytest

from agentcore.task_card_bridge import TaskCardBridge, _render_progress_bar, _truncate_summary_cleanly


def test_progress_bar_rendering():
    assert _render_progress_bar(0.0, 10) == "[□□□□□□□□□□]"
    assert _render_progress_bar(0.5, 10) == "[■■■■■□□□□□]"
    assert _render_progress_bar(1.0, 10) == "[■■■■■■■■■■]"


def test_terminal_health_counts_fenced_rows_by_profile(tmp_path):
    first = TaskCardBridge(tmp_path, profile='first')
    second = TaskCardBridge(tmp_path, profile='second')
    for bridge, task in ((first, 'a'), (first, 'b'), (second, 'c')):
        bridge.bind_task(task, {'route': 'private'}, 'private goal', 'dev', 'name', 'message')
    with first.db() as db:
        db.execute("UPDATE task_cards SET finalized=1,patch_state='unknown' WHERE task_id IN ('a','c')")
        db.execute("UPDATE task_cards SET finalized=1,patch_state='sending',updated_at=1 WHERE task_id='b'")
    assert first.terminal_health(stale_before=2) == {'unknown': 1, 'stale_sending': 1}
    assert second.terminal_health(stale_before=2) == {'unknown': 1, 'stale_sending': 0}


def test_initial_create_is_idempotent_and_preserves_terminal_state(tmp_path):
    calls = []
    bridge = TaskCardBridge(tmp_path, profile='test')
    bridge.bind_task('task', {'route': 'r', 'chat': 'c'}, 'goal', 'dev', 'name')
    bridge.finalize('task', summary='already finished')
    def creator(**kwargs):
        calls.append(kwargs)
        return {'ok': True, 'message_id': 'message'}
    result = bridge.create_initial('task', creator)
    assert result == {'ok': True, 'message_id': 'message'}
    assert calls[0]['card_payload']['header']['template'] == 'indigo'
    assert calls[0]['card_payload']['header']['icon'] == {'tag': 'standard_icon', 'token': 'myai_colorful'}
    assert bridge.get_card('task')['create_state'] == 'sent'
    assert bridge.get_card('task')['patch_state'] == 'idle'
    assert bridge.create_initial('task', creator)['already_created'] is True
    assert len(calls) == 1


def test_unknown_initial_create_is_fenced_across_restart(tmp_path):
    calls = []
    def creator(**kwargs):
        calls.append(kwargs)
        raise TimeoutError('details must not escape')
    bridge = TaskCardBridge(tmp_path, profile='test')
    bridge.bind_task('task', {'route': 'r'}, 'goal', 'dev', 'name')
    assert bridge.create_initial('task', creator)['error'] == 'create_outcome_unknown'
    restarted = TaskCardBridge(tmp_path, profile='test')
    assert restarted.create_initial('task', creator)['error'] == 'previous_create_unknown'
    assert restarted.get_card('task')['create_state'] == 'unknown'
    assert len(calls) == 1


def test_definite_unsent_initial_create_can_retry_with_same_key(tmp_path):
    calls = []
    def creator(**kwargs):
        calls.append(kwargs)
        return {'ok': False, 'not_sent': True} if len(calls) == 1 else {'ok': True, 'message_id': 'm'}
    bridge = TaskCardBridge(tmp_path, profile='test')
    bridge.bind_task('task', {'route': 'r'}, 'goal', 'dev', 'name')
    assert bridge.create_initial('task', creator)['error'] == 'create_not_sent'
    assert bridge.create_initial('task', creator)['message_id'] == 'm'
    assert calls[0]['delivery_key'] == calls[1]['delivery_key']


@pytest.mark.parametrize('response', [
    {'ok': True, 'not_sent': True, 'message_id': 'm'},
    {'ok': False, 'not_sent': True, 'message_id': 'm'},
    {'not_sent': True},
    {'ok': True, 'not_sent': 'yes', 'message_id': 'm'},
])
def test_contradictory_initial_receipt_is_unknown_and_fenced(tmp_path, response):
    calls = []
    bridge = TaskCardBridge(tmp_path, profile='p')
    bridge.bind_task('task', {'route': 'r'}, 'goal', 'dev', 'name')
    creator = lambda **kw: calls.append(kw) or response
    assert bridge.create_initial('task', creator)['error'] == 'create_outcome_unknown'
    assert bridge.get_card('task')['create_state'] == 'unknown'
    assert bridge.create_initial('task', creator)['error'] == 'previous_create_unknown'
    assert len(calls) == 1


@pytest.mark.parametrize('response', [
    {'ok': True, 'not_sent': True, 'message_id': 'm'},
    {'ok': False, 'not_sent': True, 'message_id': 'm'},
    {'not_sent': True},
    {'ok': True, 'not_sent': 'yes', 'message_id': 'm'},
])
def test_contradictory_patch_receipt_is_unknown_and_fenced(tmp_path, response):
    calls = []
    bridge = TaskCardBridge(tmp_path, profile='p', sender=lambda **kw: calls.append(kw) or response,
                            min_update_interval=0)
    bridge.bind_task('task', {'route': 'r'}, 'goal', 'dev', 'name', 'm')
    result = bridge.update_progress('task')
    assert result['patch_result'] == {'ok': False, 'error': 'patch_outcome_unknown'}
    assert bridge.get_card('task')['patch_state'] == 'unknown'
    assert bridge.update_progress('task')['patch_result']['error'] == 'previous_patch_unknown'
    assert len(calls) == 1


def test_simulated_crash_after_initial_send_stays_fenced(tmp_path):
    bridge = TaskCardBridge(tmp_path, profile='test')
    bridge.bind_task('task', {'route': 'r'}, 'goal', 'dev', 'name')
    def killed(**kwargs):
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        bridge.create_initial('task', killed)
    restarted = TaskCardBridge(tmp_path, profile='test')
    assert restarted.get_card('task')['create_state'] == 'sending'
    assert restarted.create_initial('task', lambda **kw: pytest.fail('must not resend'))['error'] == 'previous_create_unknown'


def test_parallel_initial_creates_send_only_once(tmp_path):
    bridge = TaskCardBridge(tmp_path, profile='test')
    bridge.bind_task('task', {'route': 'r'}, 'goal', 'dev', 'name')
    calls = []
    def creator(**kwargs):
        calls.append(kwargs)
        time.sleep(.05)
        return {'ok': True, 'message_id': 'm'}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: bridge.create_initial('task', creator), range(2)))
    assert all(item['ok'] and item['message_id'] == 'm' for item in results)
    assert len(calls) == 1


def test_legacy_unbound_card_is_not_assumed_unsent(tmp_path):
    bridge = TaskCardBridge(tmp_path, profile='test')
    bridge.bind_task('unknown', {'route': 'r'}, 'goal', 'dev', 'name')
    bridge.bind_task('bound', {'route': 'r'}, 'goal', 'dev', 'name', 'm')
    with bridge.db() as db:
        db.execute('ALTER TABLE task_cards DROP COLUMN create_state')
    migrated = TaskCardBridge(tmp_path, profile='test')
    assert migrated.get_card('unknown')['create_state'] == 'unknown'
    assert migrated.get_card('bound')['create_state'] == 'sent'
    assert migrated.create_initial('unknown', lambda **kw: pytest.fail('must not resend'))['error'] == 'previous_create_unknown'
    with migrated.db() as db:
        assert db.execute('PRAGMA synchronous').fetchone()[0] == 2


def test_task_card_bridge_lifecycle(tmp_path: Path):
    patches = []

    def mock_sender(address, message_id, card_payload):
        patches.append({"address": address, "message_id": message_id, "payload": card_payload})
        return {"ok": True, "message_id": message_id}

    bridge = TaskCardBridge(tmp_path / "cards", profile="test-profile", sender=mock_sender,
                            min_update_interval=0.01)

    task_id = "0123456789abcdef0123456789abcdef"
    origin = {"route": "agent:main:feishu:group:test_chat:test_sub", "chat": "oc_test", "channel": "feishu"}
    goal = "Investigate system performance and render arch docs"

    # 1. Bind task card
    res = bridge.bind_task(task_id, origin, goal, role="dev", display_name="研发执行", message_id="om_initial_001")
    assert res["ok"] is True
    assert res["message_id"] == "om_initial_001"
    card = res["card_payload"]
    assert card["schema"] == "2.0"
    assert card["config"]["wide_screen_mode"] is True
    assert card["header"]["template"] == "wathet"
    assert card["header"]["icon"] == {"tag": "standard_icon", "token": "myai_colorful"}
    assert card["header"]["subtitle"]["content"] == "Super Agent 企业智能助手"
    assert card["body"]["elements"][0]["tag"] == "column_set"
    assert card["body"]["elements"][0]["background_style"] == "turquoise-50"
    assert "研发执行" in card["body"]["elements"][0]["columns"][0]["elements"][0]["content"]

    # Verify query
    saved = bridge.get_card(task_id)
    assert saved is not None
    assert saved["status"] == "queued"
    assert saved["message_id"] == "om_initial_001"

    # 2. Update progress
    time.sleep(0.02)
    prog_res = bridge.update_progress(
        task_id, status="running", phase="依赖扫描与环境取证", progress=0.35, elapsed=12.5
    )
    assert prog_res["ok"] is True
    assert len(patches) == 1
    assert patches[0]["message_id"] == "om_initial_001"
    assert "依赖扫描与环境取证" in patches[0]["payload"]["body"]["elements"][0]["columns"][0]["elements"][0]["content"]

    # 3. Finalize card upon success (canonical indigo + blue-50 industrial contract)
    time.sleep(0.02)
    fin_res = bridge.finalize(
        task_id,
        status="completed",
        summary="全链路零 LangGraph 依赖，编排四层均为自研模块。",
        artifacts=["/tmp/report.html", "/tmp/preview.png"],
        elapsed=45.2,
    )
    assert fin_res["ok"] is True
    assert len(patches) == 2
    final_card = patches[1]["payload"]
    assert final_card["header"]["template"] == "indigo"
    assert final_card["header"]["icon"] == {"tag": "standard_icon", "token": "myai_colorful"}
    assert final_card["body"]["elements"][0]["background_style"] == "blue-50"
    assert final_card["body"]["elements"][1]["background_style"] == "grey-50"
    main_md = final_card["body"]["elements"][0]["columns"][0]["elements"][0]["content"]
    assert "全链路零 LangGraph 依赖" in main_md
    assert "report.html" in main_md

    saved_final = bridge.get_card(task_id)
    assert saved_final["finalized"] == 1
    assert saved_final["status"] == "completed"


def test_rebinding_and_late_updates_cannot_reset_a_finalized_card(tmp_path):
    calls = []
    bridge = TaskCardBridge(tmp_path / 'cards', profile='test',
                            sender=lambda *args, **kw: calls.append(kw) or {'ok': True, 'message_id': kw['message_id']},
                            min_update_interval=0)
    origin = {'route': 'route', 'chat': 'chat', 'channel': 'feishu'}
    assert bridge.bind_task('task', origin, 'goal', 'dev', 'name', 'message')['ok']
    assert bridge.finalize('task', summary='done')['ok']
    before = bridge.get_card('task')
    assert bridge.update_progress('task', status='running')['error'] == 'already_finalized'
    assert bridge.finalize('task', status='failed')['error'] == 'already_finalized'
    rebound = bridge.bind_task('task', origin, 'goal', 'dev', 'name')
    assert rebound['ok'] and rebound['message_id'] == 'message'
    assert bridge.get_card('task') == before
    assert len(calls) == 1
    assert bridge.bind_task('task', dict(origin, chat='different'), 'goal', 'dev', 'name')['error'] == 'binding_conflict'
    assert bridge.bind_task('task', origin, 'goal', 'dev', 'name', 'other')['error'] == 'message_id_conflict'
    assert bridge.set_message_id('task', 'other') is False


def test_failed_patch_does_not_throttle_a_later_attempt(tmp_path):
    responses = iter([{'ok': False, 'not_sent': True}, {'ok': True, 'message_id': 'message'}])
    bridge = TaskCardBridge(tmp_path / 'cards', profile='test',
                            sender=lambda **kwargs: next(responses), min_update_interval=60)
    bridge.bind_task('task', {'route': 'route'}, 'goal', 'dev', 'name', 'message')
    assert bridge.update_progress('task', phase='first')['patch_result']['ok'] is False
    assert bridge.update_progress('task', phase='second')['patch_result']['ok'] is True


def test_unknown_patch_is_fenced_across_restart_and_terminal_update(tmp_path):
    calls = []
    def unknown(**kwargs):
        calls.append(kwargs)
        raise TimeoutError('sensitive transport details')
    root = tmp_path / 'cards'
    first = TaskCardBridge(root, profile='test', sender=unknown, min_update_interval=0)
    first.bind_task('task', {'route': 'route'}, 'goal', 'dev', 'name', 'message')
    result = first.update_progress('task', phase='working')
    assert result['patch_result'] == {'ok': False, 'error': 'patch_outcome_unknown', 'error_type': 'TimeoutError'}
    assert first.get_card('task')['patch_state'] == 'unknown'
    restarted = TaskCardBridge(root, profile='test', sender=unknown, min_update_interval=0)
    # Architectural fix: a progress timeout must NEVER block terminal finalization!
    # Finalize attempts the terminal patch instead of being deadlocked by progress heartbeat.
    terminal = restarted.finalize('task', summary='done')
    assert terminal['patch_result']['error'] == 'patch_outcome_unknown'
    assert restarted.get_card('task')['status'] == 'completed'
    assert len(calls) == 2
    assert restarted.get_card('task')['finalized'] == 0
    assert restarted.get_card('task')['terminal_retries'] == 1


def test_definitely_unsent_terminal_can_retry_once_with_same_payload(tmp_path):
    calls = []
    def sender(**kwargs):
        calls.append(kwargs)
        return ({'ok': False, 'not_sent': True} if len(calls) == 1
                else {'ok': True, 'message_id': 'message'})
    root = tmp_path / 'cards'
    bridge = TaskCardBridge(root, profile='test', sender=sender)
    bridge.bind_task('task', {'route': 'route'}, 'goal', 'dev', 'name', 'message')
    first = bridge.finalize('task', status='failed', summary='failed', error_msg='details')
    assert first['patch_result']['not_sent'] and bridge.get_card('task')['patch_state'] == 'unsent'
    restarted = TaskCardBridge(root, profile='test', sender=sender)
    assert restarted.finalize('task', status='failed', summary='changed', error_msg='details')['error'] == 'already_finalized'
    retried = restarted.finalize('task', status='failed', summary='failed', error_msg='details')
    assert retried['patch_result']['ok'] and restarted.get_card('task')['patch_state'] == 'idle'
    assert restarted.finalize('task', status='failed', summary='failed', error_msg='details')['already_finalized']
    assert len(calls) == 2


def test_terminal_patch_bounded_retry_and_degradation(tmp_path):
    calls = []
    def failing_sender(**kwargs):
        calls.append(kwargs)
        return {'ok': False, 'error': 'timeout'}

    root = tmp_path / 'cards'
    bridge = TaskCardBridge(root, profile='test', sender=failing_sender)
    bridge.bind_task('task', {'route': 'route'}, 'goal', 'dev', 'name', 'message')

    # Attempt 1
    r1 = bridge.finalize('task', status='completed', summary='done')
    assert r1['ok'] is True and r1['finalized'] is False
    assert bridge.get_card('task')['terminal_retries'] == 1
    assert bridge.get_card('task')['patch_state'] == 'unknown'

    # Attempt 2
    r2 = bridge.finalize('task', status='completed', summary='done')
    assert r2['ok'] is True and r2['finalized'] is False
    assert bridge.get_card('task')['terminal_retries'] == 2

    # Attempt 3: hits max retries -> marks degraded terminal state
    r3 = bridge.finalize('task', status='completed', summary='done')
    assert r3['ok'] is True and r3.get('degraded') is True
    assert bridge.get_card('task')['terminal_retries'] == 3
    assert bridge.get_card('task')['patch_state'] == 'degraded'
    assert bridge.get_card('task')['finalized'] == 1

    # Subsequent call recognizes degraded terminal state without re-sending
    r4 = bridge.finalize('task', status='completed', summary='done')
    assert r4['ok'] is True and r4.get('degraded') is True
    assert len(calls) == 3


def test_stale_sending_terminal_patch_recovers_after_timeout(tmp_path):
    calls = []
    def sender(**kwargs):
        calls.append(kwargs)
        return {'ok': True, 'message_id': 'message'}

    root = tmp_path / 'cards'
    bridge = TaskCardBridge(root, profile='test', sender=sender)
    bridge.bind_task('task', {'route': 'route'}, 'goal', 'dev', 'name', 'message')

    # Simulate a crashed process that left patch_state='sending' with an old updated_at
    with bridge.db() as conn:
        conn.execute("UPDATE task_cards SET patch_state='sending', updated_at=? WHERE task_id='task'",
                     (time.time() - 35.0,))

    # Finalize sees the send is stale (> 30s) and proceeds to send instead of deadlocking
    res = bridge.finalize('task', status='completed', summary='done')
    assert res['ok'] is True
    assert res['patch_result'] == {'ok': True, 'message_id': 'message'}
    assert bridge.get_card('task')['patch_state'] == 'idle'
    assert bridge.get_card('task')['finalized'] == 1
    assert len(calls) == 1


def test_terminal_without_message_can_be_delivered_after_binding(tmp_path):
    calls = []
    bridge = TaskCardBridge(tmp_path / 'cards', profile='test',
                            sender=lambda **kw: calls.append(kw) or {'ok': True, 'message_id': kw['message_id']})
    bridge.bind_task('task', {'route': 'route'}, 'goal', 'dev', 'name')
    assert bridge.finalize('task', summary='done')['ok']
    assert bridge.get_card('task')['patch_state'] == 'unsent'
    assert bridge.set_message_id('task', 'message')
    assert bridge.finalize('task', summary='done')['patch_result']['ok']
    assert len(calls) == 1


def test_unfinished_patch_marker_is_fenced_after_restart(tmp_path):
    root = tmp_path / 'cards'
    bridge = TaskCardBridge(root, profile='test')
    bridge.bind_task('task', {'route': 'route'}, 'goal', 'dev', 'name', 'message')
    with bridge.db() as db:
        db.execute("UPDATE task_cards SET patch_state='sending' WHERE task_id='task'")
    calls = []
    restarted = TaskCardBridge(root, profile='test', sender=lambda **kw: calls.append(kw))
    result = restarted.update_progress('task', phase='working')
    assert result['patch_result']['error'] == 'previous_patch_unknown'
    assert calls == []


def test_mismatched_patch_receipt_is_unknown_not_success(tmp_path):
    bridge = TaskCardBridge(tmp_path / 'cards', profile='test',
                            sender=lambda **kw: {'ok': True, 'message_id': 'different'})
    bridge.bind_task('task', {'route': 'route'}, 'goal', 'dev', 'name', 'message')
    result = bridge.update_progress('task')
    assert result['patch_result']['error'] == 'patch_receipt_mismatch'
    assert bridge.get_card('task')['patch_state'] == 'unknown'


def test_patch_success_without_message_id_is_not_confirmation(tmp_path):
    bridge = TaskCardBridge(tmp_path / 'cards', profile='test', sender=lambda **kw: {'ok': True})
    bridge.bind_task('task', {'route': 'route'}, 'goal', 'dev', 'name', 'message')
    assert bridge.update_progress('task')['patch_result']['error'] == 'patch_receipt_mismatch'
    assert bridge.get_card('task')['patch_state'] == 'unknown'


def test_task_lock_wait_is_bounded(tmp_path):
    bridge = TaskCardBridge(tmp_path / 'cards', profile='test', lock_timeout=.05)
    lock_dir = bridge.root / 'locks'
    lock_dir.mkdir()
    stripe = hashlib.sha256(b'task').digest()[0] % 64
    fd = os.open(lock_dir / ('%02x.lock' % stripe), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with pytest.raises(TimeoutError, match='task_card_lock_timeout'):
            bridge.bind_task('task', {'route': 'route'}, 'goal', 'dev', 'name')
    finally:
        os.close(fd)


def test_throttle_cache_is_bounded_and_finalization_releases_entry(tmp_path, monkeypatch):
    import agentcore.task_card_bridge as module
    monkeypatch.setattr(module, '_MAX_THROTTLE_ENTRIES', 2)
    bridge = TaskCardBridge(tmp_path / 'cards', profile='test', min_update_interval=60,
                            sender=lambda **kw: {'ok': True, 'message_id': kw['message_id']})
    for task in ('one', 'two', 'three'):
        bridge.bind_task(task, {'route': 'route'}, 'goal', 'dev', 'name', task)
        assert bridge.update_progress(task)['patch_result']['ok']
    assert len(bridge._last_patched) == 2 and 'one' not in bridge._last_patched
    assert bridge.finalize('three', summary='done')['patch_result']['ok']
    assert 'three' not in bridge._last_patched


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), -1, 2, True, 10**1000])
def test_invalid_progress_does_not_start_patch(tmp_path, bad):
    calls = []
    bridge = TaskCardBridge(tmp_path / 'cards', profile='test', sender=lambda **kw: calls.append(kw))
    bridge.bind_task('task', {'route': 'route'}, 'goal', 'dev', 'name', 'message')
    assert bridge.update_progress('task', progress=bad)['error'] == 'invalid_progress_values'
    assert bridge.get_card('task')['patch_state'] == 'idle'
    assert calls == []


def test_invalid_artifact_input_does_not_finalize(tmp_path):
    bridge = TaskCardBridge(tmp_path / 'cards', profile='test')
    bridge.bind_task('task', {'route': 'route'}, 'goal', 'dev', 'name', 'message')
    assert bridge.finalize('task', artifacts=[None])['error'] == 'invalid_artifacts'
    assert bridge.get_card('task')['finalized'] == 0


def test_old_card_database_is_migrated_without_losing_state(tmp_path):
    root = tmp_path / 'cards'
    bridge = TaskCardBridge(root, profile='test')
    bridge.bind_task('task', {'route': 'route'}, 'goal', 'dev', 'name', 'message')
    with bridge.db() as db:
        db.execute('DROP INDEX task_card_terminal_health')
        db.execute('ALTER TABLE task_cards DROP COLUMN patch_state')
        db.execute('ALTER TABLE task_cards DROP COLUMN thread_id')
        db.execute('ALTER TABLE task_cards DROP COLUMN error_text')
    restarted = TaskCardBridge(root, profile='test')
    saved = restarted.get_card('task')
    assert saved['message_id'] == 'message' and saved['patch_state'] == 'unknown' and saved['thread_id'] == '' and saved['error_text'] == ''


def test_card_does_not_include_local_absolute_paths(tmp_path):
    bridge = TaskCardBridge(tmp_path / 'cards', profile='test')
    card = bridge.build_card_payload('task', 'name', '/home/测试用户/目标', 'completed',
        summary='结果：/home/测试用户/报告.txt', artifacts=['/tmp/private/报告.txt'])
    text = card['body']['elements'][0]['columns'][0]['elements'][0]['content']
    assert '/home/' not in text and '/tmp/' not in text and '测试用户' not in text
    assert '报告.txt' in text


def test_other_profile_cannot_read_or_change_a_card(tmp_path):
    root = tmp_path / 'cards'
    owner = TaskCardBridge(root, profile='owner')
    other = TaskCardBridge(root, profile='other')
    owner.bind_task('task', {'route': 'route'}, 'goal', 'dev', 'name', 'message')
    assert other.get_card('task') is None
    assert other.set_message_id('task', 'other-message') is False
    assert other.update_progress('task')['error'] == 'task_not_found'
    assert other.finalize('task')['error'] == 'task_not_found'
    assert owner.get_card('task')['status'] == 'queued'


def test_thread_binding_is_persisted_and_cannot_be_rebound(tmp_path):
    calls = []
    bridge = TaskCardBridge(tmp_path / 'cards', profile='test',
                            sender=lambda **kw: calls.append(kw) or {'ok': True, 'message_id': kw['message_id']}, min_update_interval=0)
    origin = {'route': 'route', 'chat': 'chat', 'thread': 'thread-1'}
    assert bridge.bind_task('task', origin, 'goal', 'dev', 'name', 'message')['ok']
    assert bridge.bind_task('task', dict(origin, thread='thread-2'), 'goal', 'dev', 'name')['error'] == 'binding_conflict'
    assert bridge.update_progress('task')['patch_result']['ok']
    assert calls[0]['address']['thread'] == 'thread-1'


def test_concurrent_instances_do_not_patch_progress_after_terminal(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    final_sent = threading.Event()
    events = []
    def sender(address, message_id, card_payload):
        title = card_payload['header']['title']['content']
        if '在进行' in title:
            events.append('progress_started')
            entered.set()
            assert release.wait(3)
            events.append('progress_finished')
        else:
            events.append('final')
            final_sent.set()
        return {'ok': True, 'message_id': message_id}
    root = tmp_path / 'cards'
    first = TaskCardBridge(root, profile='test', sender=sender, min_update_interval=0)
    second = TaskCardBridge(root, profile='test', sender=sender, min_update_interval=0)
    first.bind_task('task', {'route': 'route'}, 'goal', 'dev', 'name', 'message')
    progress = threading.Thread(target=lambda: first.update_progress('task', phase='working'))
    final = threading.Thread(target=lambda: second.finalize('task', summary='done'))
    progress.start()
    try:
        assert entered.wait(2)
        final.start()
        assert not final_sent.wait(.1)
    finally:
        release.set()
        progress.join(3)
        if final.ident is not None:
            final.join(3)
    assert not progress.is_alive() and not final.is_alive()
    assert events == ['progress_started', 'progress_finished', 'final']
    assert first.get_card('task')['status'] == 'completed'


def test_task_card_bridge_sender_fault_tolerance(tmp_path: Path):
    def failing_sender(address, message_id, card_payload):
        raise ConnectionResetError("network disconnected")

    bridge = TaskCardBridge(tmp_path / "cards", profile="test-profile", sender=failing_sender)
    task_id = "abcdef0123456789abcdef0123456789"
    bridge.bind_task(task_id, {"route": "route1"}, "test goal", "analysis", "证据分析", message_id="om_fail_01")

    # Should not raise exception
    res = bridge.update_progress(task_id, status="running", phase="test", force_sync=True)
    assert res["ok"] is True
    assert res["patch_result"]["ok"] is False
    assert res["patch_result"]["error_type"] == "ConnectionResetError"

def test_sanitize_card_text_unescapes_unicode_and_unwraps_json():
    from agentcore.task_card_bridge import _sanitize_card_text

    # Case 1: Raw escaped unicode
    assert _sanitize_card_text("核验：\\u6838\\u9a8c") == "核验：核验"

    # Case 2: JSON string containing unicode escape
    raw_json = '{"ok": true, "result": {"summary": "host-node \\u6838\\u9a8c\\uff1a\\u901a\\u8fc7"}}'
    assert _sanitize_card_text(raw_json) == "host-node 核验：通过"

    # Case 3: Prefix with JSON string
    mixed = '结果: {"ok": true, "result": {"summary": "\\u6210\\u529f"}}'
    assert _sanitize_card_text(mixed) == "结果:\n成功"


def test_single_source_of_truth_for_cardkit2_schema():
    """Architectural gate: active CardKit 2.0 builders must delegate to cardkit2_renderer with zero exceptions."""
    repo_root = Path(__file__).resolve().parent.parent
    offenders = []
    for sub in ("agentcore", "plugins", "runtime", "tools"):
        for py in (repo_root / sub).rglob("*.py"):
            rel = py.relative_to(repo_root).as_posix()
            if rel == "agentcore/cardkit2_renderer.py":
                continue
            text = py.read_text(encoding="utf-8")
            for idx, line in enumerate(text.splitlines(), 1):
                if '"schema": "2.0"' in line or "'schema': '2.0'" in line:
                    offenders.append(f"{rel}:{idx}")
    assert offenders == [], f"Bespoke CardKit 2.0 schema construction found outside canonical renderer: {offenders}"


def test_no_raw_pool_db_sql_leakage():
    """Architectural gate: all modules in agentcore/ (except pool.py), plugins/, and runtime/ must use Pool contract methods, never raw SQL on pool.db."""
    repo_root = Path(__file__).resolve().parent.parent
    forbidden_patterns = (
        "pool.db()",
        "root_pool.db()",
        "durable_pool.db()",
        "FROM card_outbox",
        "UPDATE card_outbox",
        "INTO card_outbox",
        "FROM outbox",
        "UPDATE outbox",
        "INTO outbox",
        "FROM origin_repairs",
        "FROM jobs",
        "UPDATE jobs",
        "FROM ops_alert_outbox",
        "UPDATE ops_alert_outbox",
        "INTO ops_alert_outbox",
        "FROM ops_alert_episodes",
        "FROM ops_health_conditions",
        "FROM readonly_admissions",
        "UPDATE readonly_admissions",
        "INTO readonly_admissions",
        "FROM readonly_deliveries",
        "UPDATE readonly_deliveries",
        "INTO readonly_deliveries",
    )
    offenders = []
    guarded_files = [
        *(repo_root / "agentcore").rglob("*.py"),
        *(repo_root / "plugins").rglob("*.py"),
        *(repo_root / "runtime").rglob("*.py"),
    ]
    for py in guarded_files:
        rel = py.relative_to(repo_root).as_posix()
        if rel == "agentcore/pool.py":
            continue
        text = py.read_text(encoding="utf-8")
        for idx, line in enumerate(text.splitlines(), 1):
            for pat in forbidden_patterns:
                if pat in line:
                    offenders.append(f"{rel}:{idx} ({pat})")
    assert offenders == [], f"Cross-module raw SQL against pool.db found in guarded modules: {offenders}"


def test_no_raw_pool_db_sql_leakage_in_plugins_or_runtime():
    test_no_raw_pool_db_sql_leakage()


def test_single_source_of_truth_for_unicode_unescape():
    """Architectural gate: unicode escape sequence unwrapping must reside solely in delivery_sanitizer.py."""
    repo_root = Path(__file__).resolve().parent.parent
    offenders = []
    for sub in ("agentcore", "plugins", "runtime", "tools"):
        for py in (repo_root / sub).rglob("*.py"):
            rel = py.relative_to(repo_root).as_posix()
            if rel == "agentcore/delivery_sanitizer.py":
                continue
            text = py.read_text(encoding="utf-8")
            for idx, line in enumerate(text.splitlines(), 1):
                if "0-9a-fA-F]{4}" in line:
                    offenders.append(f"{rel}:{idx}")
    assert offenders == [], f"Duplicated unicode unescape regex found outside delivery_sanitizer.py: {offenders}"


def test_truncate_summary_cleanly_short_text():
    short = "这是不超过1500字符的简短执行结论。"
    assert _truncate_summary_cleanly(short) == short


def test_truncate_summary_cleanly_paragraph_boundary():
    p1 = "段落一内容。" * 70  # ~420 chars
    p2 = "段落二内容。" * 70  # ~420 chars (total ~840 chars)
    p3 = "段落三长文。" * 120  # ~720 chars (total > 1500 chars)
    full = f"{p1}\n\n{p2}\n\n{p3}"
    assert len(full) > 1500

    res = _truncate_summary_cleanly(full)
    assert res.endswith("*(长文本详细内容已作为附件发送至会话)*")
    assert f"{p1}\n\n{p2}" in res
    assert p3 not in res


def test_truncate_summary_cleanly_newline_boundary():
    line1 = "单行文本内容。" * 140  # ~980 chars
    line2 = "后续换行内容。" * 100  # total > 1500 chars
    full = f"{line1}\n{line2}"
    assert len(full) > 1500

    res = _truncate_summary_cleanly(full)
    assert res.endswith("*(长文本详细内容已作为附件发送至会话)*")
    assert line1 in res


def test_truncate_summary_cleanly_chinese_punctuation_boundary():
    # Long text on a single line without newlines
    sentence1 = "甲" * 800 + "。"
    sentence2 = "乙" * 800
    full = sentence1 + sentence2
    assert len(full) > 1500

    res = _truncate_summary_cleanly(full)
    assert res.endswith("*(长文本详细内容已作为附件发送至会话)*")
    assert sentence1 in res
    assert "乙" * 800 not in res


def test_truncate_summary_cleanly_seals_unclosed_fence():
    p1 = "前置文本。" * 60  # ~300 chars
    code_start = "\n```python\ndef process_data():\n"
    code_body = "    row = fetch_next_record()\n" * 30  # ~900 chars
    rest = "结尾补充说明。" * 100
    full = f"{p1}{code_start}{code_body}{rest}"
    assert len(full) > 1500

    res = _truncate_summary_cleanly(full)
    assert res.endswith("*(长文本详细内容已作为附件发送至会话)*")
    # Must have closing fence sealed cleanly before the overflow notice
    body_without_note = res.split("\n\n*(长文本详细内容已作为附件发送至会话)*")[0]
    assert body_without_note.rstrip().endswith("```")


def test_build_card_payload_with_docx_url(tmp_path):
    bridge = TaskCardBridge(tmp_path, profile="test")
    payload = bridge.build_card_payload(
        "task_docx_001",
        "文档测试助手",
        "验证云文档链接",
        "completed",
        summary="测试完成，详细文档已生成。",
        docx_url="https://feishu.cn/docx/sample_doc_token",
    )
    elements = payload["body"]["elements"]
    main_text = elements[0]["columns"][0]["elements"][0]["content"]
    assert "**在线文档**：[点击打开完整云文档](https://feishu.cn/docx/sample_doc_token)" in main_text


def test_build_card_payload_with_hero_img_key(tmp_path):
    bridge = TaskCardBridge(tmp_path, profile="test")
    payload = bridge.build_card_payload(
        "task_hero_001",
        "图像测试助手",
        "验证封面预览图",
        "completed",
        summary="测试完成。",
        hero_img_key="img_v2_sample_hero_token",
    )
    elements = payload["body"]["elements"]
    img_element = next((el for el in elements if el.get("tag") == "img"), None)
    assert img_element is not None
    assert img_element["img_key"] == "img_v2_sample_hero_token"
    assert img_element["preview"] is True
    assert img_element["alt"]["content"] == "预览图"


def test_finalize_passes_hero_img_and_docx_url(tmp_path):
    bridge = TaskCardBridge(tmp_path, profile="test")
    bridge.bind_task("task_fin_001", {"route": "r", "chat": "c"}, "完成测试", "dev", "开发")
    res = bridge.finalize(
        "task_fin_001",
        summary="成功收敛全部边界条件。",
        hero_img_key="img_hero_999",
        docx_url="https://feishu.cn/docx/summary_token",
    )
    assert res["ok"] is True
    card = res["card_payload"]
    elements = card["body"]["elements"]

    main_text = elements[0]["columns"][0]["elements"][0]["content"]
    assert "[点击打开完整云文档](https://feishu.cn/docx/summary_token)" in main_text

    img_element = next((el for el in elements if el.get("tag") == "img"), None)
    assert img_element is not None
    assert img_element["img_key"] == "img_hero_999"

