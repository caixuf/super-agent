import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from agentcore.pool import Pool


def test_concurrent_dedupe_and_claim(tmp_path):
    pool = Pool(tmp_path)
    with ThreadPoolExecutor(8) as executor:
        ids = list(executor.map(lambda _: pool.submit('one', {'goal': 'check'}), range(16)))
    assert len(set(ids)) == 1
    with ThreadPoolExecutor(8) as executor:
        claims = list(executor.map(lambda _: pool.claim(), range(8)))
    winners = [c for c in claims if c]
    assert len(winners) == 1
    os.close(winners[0][1])


def test_dependency_failure_and_fenced_finish(tmp_path):
    p = Pool(tmp_path)
    first = p.submit('a', {})
    second = p.submit('b', {}, [first])
    job, fd = p.claim()
    assert job['id'] == first
    assert not p.finish(first, 'stale-token', True, {})
    assert p.finish(first, job['attempt'], False, {'reason': 'failure'})
    os.close(fd)
    assert p.claim() is None
    assert p.status(second)['status'] == 'failed'
    assert len(p.receipts()) == 2
    p.acknowledge(first)
    assert len(p.receipts()) == 1


def test_expired_lease_does_not_overlap_surviving_child(tmp_path):
    p = Pool(tmp_path)
    task = p.submit('a', {}, replay_safe=True)
    job, fd = p.claim()
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'], pass_fds=(fd,))
    os.close(fd)
    try:
        assert p.claim(lease=0) is None
    finally:
        child.kill()
        child.wait()
    recovered, newfd = p.claim(lease=0)
    assert recovered['id'] == task
    assert recovered['attempt'] != job['attempt']
    assert not p.finish(task, job['attempt'], True, {})
    os.close(newfd)


def test_interrupted_unsafe_task_not_replayed(tmp_path):
    p = Pool(tmp_path)
    task = p.submit('a', {})
    _, fd = p.claim()
    os.close(fd)
    assert p.claim(lease=0) is None
    assert p.status(task)['status'] == 'failed'


def test_runner_receipts_timeout_and_cancel(tmp_path):
    p = Pool(tmp_path)
    task = p.submit('a', {})
    assert p.run_once([sys.executable, '-c', 'import json; print(json.dumps({"ok":True,"result":"evidence"}))'])
    assert p.status(task)['status'] == 'completed'
    bad = p.submit('b', {})
    p.run_once([sys.executable, '-c', 'print("not a receipt")'])
    assert p.status(bad)['status'] == 'failed'
    slow = p.submit('c', {})
    p.run_once([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=0.1)
    assert p.status(slow)['status'] == 'failed'
    cancelled = p.submit('d', {})
    p.cancel(cancelled)
    assert p.claim() is None
    assert p.status(cancelled)['status'] == 'cancelled'


def test_retry_budget_and_dependency_success(tmp_path):
    p = Pool(tmp_path)
    task = p.submit('a', {}, replay_safe=True, max_tries=2)
    child = p.submit('b', {}, [task])
    p.run_once([sys.executable, '-c', 'raise SystemExit(1)'])
    assert p.status(task)['status'] == 'queued'
    p.run_once([sys.executable, '-c', 'print("{\\"ok\\":true}")'])
    assert p.status(task)['status'] == 'completed'
    row, fd = p.claim()
    assert row['id'] == child
    os.close(fd)
