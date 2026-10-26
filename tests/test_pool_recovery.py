"""Process-level recovery checks; all runners and databases use pytest tmp_path."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from agentcore.pool import Pool


pytestmark = pytest.mark.skipif(not Path('/proc/self/stat').exists(), reason='Linux /proc required')
REPO = Path(__file__).resolve().parents[1]


def _wait_until(check, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(.02)
    raise AssertionError('condition did not settle before deadline')


def _alive(pid):
    try:
        state = Path('/proc/{}/stat'.format(pid)).read_text().rsplit(')', 1)[1].split()[0]
        return state != 'Z'
    except FileNotFoundError:
        return False


def _kill_owned_group(pid):
    if pid and _alive(pid):
        assert os.getpgid(pid) == pid
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _row(pool, job):
    with pool.db() as db:
        value = db.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone()
        return dict(value) if value else None


def test_exited_runner_stderr_cannot_bypass_output_budget(tmp_path):
    p = Pool(tmp_path)
    job = p.submit('stderr', {})
    code = 'import json,sys; sys.stderr.write("x"*8192); sys.stderr.flush(); print(json.dumps({"ok":True}))'
    p.run_once([sys.executable, '-c', code], max_output=1024)
    state = p.status(job)
    assert state['status'] == 'failed'
    assert json.loads(state['result'])['reason'] == 'output budget exceeded'
    assert len(p.receipts()) == 1


@pytest.mark.parametrize('change', [{'replay_safe': True}, {'max_tries': 3}])
def test_dedupe_rejects_execution_policy_changes(tmp_path, change):
    p = Pool(tmp_path)
    p.submit('same', {}, replay_safe=False, max_tries=2)
    with pytest.raises(ValueError, match='different task'):
        p.submit('same', {}, **change)


def test_attempt_directory_failure_releases_job_lock(tmp_path):
    p = Pool(tmp_path)
    job = p.submit('mkdir', {})
    claimed = p.claim()
    attempt = claimed[0]['attempt']
    (tmp_path / 'attempts' / attempt).mkdir()
    p.run_claimed(claimed, [sys.executable, '-c', 'print("unused")'])
    assert p.status(job)['status'] == 'failed'
    import fcntl
    with (tmp_path / 'locks' / job).open('r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_running_cancel_stops_runner_and_commits_one_receipt(tmp_path):
    p = Pool(tmp_path)
    job = p.submit('cancel-running', {})
    code = 'import os,time; from pathlib import Path; Path("pid").write_text(str(os.getpid())); time.sleep(30)'
    runner_pid = None
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(p.run_once, [sys.executable, '-c', code], 20, .2)
        try:
            record = _wait_until(lambda: next(iter(tmp_path.glob('attempts/*/pid')), None))
            runner_pid = int(record.read_text())
            assert p.status(job)['status'] == 'running'
            p.cancel(job)
            assert future.result(timeout=5)
            assert p.status(job)['status'] == 'cancelled'
            assert not _alive(runner_pid)
            assert [r['status'] for r in p.receipts()] == ['cancelled']
        finally:
            _kill_owned_group(runner_pid)
            future.result(timeout=5)


@pytest.mark.parametrize('cancel', [False, True], ids=['deadline', 'cancel'])
def test_crashed_worker_orphan_is_stopped_before_recovery(tmp_path, cancel):
    p = Pool(tmp_path)
    job = p.submit('orphan', {}, replay_safe=True)
    code = 'import time; time.sleep(30)'
    worker_code = ('from agentcore.pool import Pool; import sys; '
                   'Pool(sys.argv[1]).run_once([sys.executable,"-c",sys.argv[2]],timeout=.5,lease=.2)')
    worker = subprocess.Popen([sys.executable, '-c', worker_code, str(tmp_path), code], cwd=REPO)
    runner_pid = None
    recovered = None
    try:
        row = _wait_until(lambda: (lambda r: r if r.get('pid_start') else None)(_row(p, job)))
        runner_pid = row['pid']
        first_attempt = row['attempt']
        worker.kill()
        worker.wait(timeout=5)
        assert _alive(runner_pid)
        if cancel:
            p.cancel(job)
        else:
            _wait_until(lambda: time.time() > row['deadline'])

        def settle():
            nonlocal recovered
            result = p.claim(lease=0)
            if result:
                recovered = result
                return True
            return p.status(job)['status'] == 'cancelled'

        _wait_until(settle)
        assert not _alive(runner_pid), 'old attempt must be quiescent before claim'
        if cancel:
            assert recovered is None
            assert p.status(job)['status'] == 'cancelled'
            assert len(p.receipts()) == 1
        else:
            assert recovered is not None
            assert recovered[0]['attempt'] != first_attempt
            assert not p.finish(job, first_attempt, True, {})
            assert p.finish(job, recovered[0]['attempt'], True, {'recovered': True})
            assert p.status(job)['status'] == 'completed'
            assert len(p.receipts()) == 1
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)
        _kill_owned_group(runner_pid)
        if recovered:
            os.close(recovered[1])


def test_orphan_reaper_does_not_signal_mismatched_pid_identity(tmp_path):
    p = Pool(tmp_path)
    job = p.submit('identity-guard', {}, replay_safe=True)
    claimed = p.claim()
    lock_fd = claimed[1]
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                             start_new_session=True, pass_fds=(lock_fd,))
    os.close(lock_fd)
    try:
        with p.db() as db:
            db.execute('UPDATE jobs SET pid=?,pid_start=?,deadline=?,cancel=1 WHERE id=?',
                       (child.pid, 'not-the-starttime', time.time() - 1, job))
        assert p.claim(lease=0) is None
        assert child.poll() is None, 'a PID identity mismatch must never be signalled'
    finally:
        _kill_owned_group(child.pid)
        child.wait(timeout=5)


def test_runner_file_limit_bounds_an_individual_artifact(tmp_path):
    p = Pool(tmp_path)
    job = p.submit('file-limit', {})
    code = 'import json; from pathlib import Path; Path("artifact").write_bytes(b"x"*1048576); print(json.dumps({"ok":True}))'
    p.run_once([sys.executable, '-c', code], max_file=4096)
    assert p.status(job)['status'] == 'failed'
    artifact = next(tmp_path.glob('attempts/*/artifact'))
    assert artifact.stat().st_size <= 4096


def test_artifact_budget_is_distinct_from_stdout_budget(tmp_path):
    p = Pool(tmp_path)
    job = p.submit('artifact', {})
    code = 'import json; from pathlib import Path; Path("artifact").write_bytes(b"x"*2097152); print(json.dumps({"ok":True}))'
    p.run_once([sys.executable, '-c', code], max_output=1024, max_file=4194304)
    assert p.status(job)['status'] == 'completed'
    assert next(tmp_path.glob('attempts/*/artifact')).stat().st_size == 2097152


def test_generator_dependencies_survive_validation_and_block_claim(tmp_path):
    p = Pool(tmp_path)
    first = p.submit('parent', {})
    child = p.submit('child', {}, dependencies=(item for item in [first]))
    first_claim = p.claim()
    child_claim = None
    try:
        assert first_claim[0]['id'] == first
        assert p.claim() is None, 'a generator dependency must not disappear after validation'
        assert p.finish(first, first_claim[0]['attempt'], True, {})
        os.close(first_claim[1])
        first_claim = None
        child_claim = p.claim()
        assert child_claim[0]['id'] == child
    finally:
        if first_claim:
            os.close(first_claim[1])
        if child_claim:
            os.close(child_claim[1])
