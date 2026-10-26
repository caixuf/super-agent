import json
from pathlib import Path
import sys
import threading

import pytest

from agentcore.pool import Pool
from agentcore.worker import Worker


def runner(tmp_path):
    path = tmp_path / 'runner.py'
    path.write_text('''import json, sys, time
job = json.load(sys.stdin)
time.sleep(.15)
print(json.dumps({'ok': True, 'result': job['payload']}))
''')
    return [sys.executable, str(path)]


def test_worker_drains_dependencies_and_receipts(tmp_path):
    pool = Pool(tmp_path / 'state')
    first = pool.submit('first', {'kind': 'analysis'})
    for i in range(5):
        pool.submit(str(i), {'index': i}, dependencies=[first])
    stats = Worker(pool, runner(tmp_path), workers=3).run(drain=True)
    assert stats['attempts_finished'] == 6
    receipts = pool.receipts()
    assert len(receipts) == 6
    assert all(r['status'] == 'completed' for r in receipts)
    assert len({r['job'] for r in receipts}) == 6


def test_stop_admits_no_work(tmp_path):
    pool = Pool(tmp_path / 'state')
    job = pool.submit('job', {})
    stop = threading.Event()
    stop.set()
    stats = Worker(pool, runner(tmp_path)).run(stop)
    assert stats['attempts_finished'] == 0
    assert pool.status(job)['status'] == 'queued'


def test_config_and_unknown_backend_do_not_consume_job(tmp_path):
    pool = Pool(tmp_path / 'state')
    job = pool.submit('job', {})
    with pytest.raises(ValueError):
        Worker(pool, ['true'], workers=0)
    with pytest.raises(ValueError):
        Worker(pool, ['true'], backend='unknown').run(drain=True)
    assert pool.status(job)['status'] == 'queued'


def test_cli_drains_in_separate_process(tmp_path):
    import subprocess
    pool = Pool(tmp_path / 'state')
    task = pool.submit('job', {'goal': 'check'})
    config = tmp_path / 'runner.json'
    config.write_text(json.dumps(runner(tmp_path)))
    root = str(Path(__file__).resolve().parents[1])
    proc = subprocess.run([sys.executable, '-m', 'agentcore.pool', '--root', str(pool.root),
                           'worker', '--runner-config', str(config), '--workers', '2', '--drain'],
                          cwd=root, capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)['attempts_finished'] == 1
    assert pool.status(task)['status'] == 'completed'


def test_outbox_claim_fences_stale_consumer(tmp_path):
    pool = Pool(tmp_path / 'state')
    task = pool.submit('job', {})
    pool.run_once(runner(tmp_path))
    claim = pool.claim_receipt()
    assert claim['receipt']['job'] == task
    assert pool.claim_receipt() is None
    assert not pool.acknowledge(task)
    assert not pool.acknowledge(task, 'wrong-token')
    with pool.db() as db:
        db.execute('UPDATE outbox SET lease_until=0 WHERE job=?', (task,))
    again = pool.claim_receipt()
    assert again['token'] != claim['token']
    assert not pool.acknowledge(task, claim['token'])
    assert pool.acknowledge(task, again['token'])
    assert pool.receipts() == []


def test_native_worker_when_binding_available(tmp_path):
    pytest.importorskip('flowcoro_py')
    pool = Pool(tmp_path / 'state')
    for i in range(3):
        pool.submit(str(i), {'i': i})
    stats = Worker(pool, runner(tmp_path), workers=2, backend='flowcoro').run(drain=True)
    assert stats['attempts_finished'] == 3
    assert all(r['status'] == 'completed' for r in pool.receipts())


def test_independent_processes_claim_only_one_attempt(tmp_path):
    import subprocess
    pool = Pool(tmp_path / 'state')
    task = pool.submit('same', {})
    root = str(Path(__file__).resolve().parents[1])
    code = ('from agentcore.pool import Pool; import os,sys,json; '
            'p=Pool(sys.argv[1]); c=p.claim(); '
            'print(json.dumps(c[0]["id"] if c else None)); '
            'os.close(c[1]) if c else None')
    children = [subprocess.Popen([sys.executable, '-c', code, str(pool.root)], cwd=root,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for _ in range(3)]
    try:
        results = []
        for child in children:
            out, err = child.communicate(timeout=60)
            assert child.returncode == 0, err
            results.append(json.loads(out))
        assert results.count(task) == 1
        assert results.count(None) == 2
        assert pool.status(task)['tries'] == 1
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()
