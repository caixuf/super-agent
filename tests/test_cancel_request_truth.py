"""A control acknowledgement is not evidence that an executing attempt stopped."""
from __future__ import annotations

import os
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agentcore.barge_in_control import BargeInController
from agentcore.card_emit import CardEmitter
from agentcore.card_store import CardStore
from agentcore.pool import Pool


class RecordingTransport:
    def __init__(self):
        self.frames = []

    def emit(self, handle, view, *, final=False):
        self.frames.append((view, final))


class RecordingNotifier:
    def __init__(self):
        self.calls = []

    def notify_terminal(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"status": "empty"}


def test_finalize_cancel_does_not_terminal_a_live_runner(tmp_path):
    pool = Pool(tmp_path / "pool")
    job = pool.submit("live-cancel", {"goal": "wait"})
    row, fd = pool.claim()
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        with pool.db() as db:
            db.execute(
                "UPDATE jobs SET pid=?,pid_start=?,deadline=? WHERE id=?",
                (child.pid, pool._process_start(child.pid), time.time() + 60, job),
            )
        assert child.poll() is None
        heartbeat = pool.status(job)["heartbeat"]
        assert pool.finalize_cancel(job) is True
        assert child.poll() is None
        current = pool.status(job)
        assert current["status"] == "running"
        assert current["stage"] == "canceling"
        assert current["attempt"] == row["attempt"]
        assert current["heartbeat"] == heartbeat
        assert pool.receipts() == []
        stamp = current["cancel_requested_at"]
        assert pool.finalize_cancel(job) is True
        assert pool.status(job)["cancel_requested_at"] == stamp
    finally:
        child.kill()
        child.wait(timeout=5)
        os.close(fd)


def test_cancel_control_reports_pending_without_terminal_card_or_notification(tmp_path):
    pool = Pool(tmp_path / "pool")
    job = pool.submit("control-cancel", {"goal": "work"})
    _, fd = pool.claim()
    cards = CardStore(tmp_path / "cards.db")
    cards.bind(job, "card-1", "message-1")
    transport = RecordingTransport()
    emitter = CardEmitter(cards, job, transport)
    emitter.open(phase="work")
    notifier = RecordingNotifier()
    try:
        result = BargeInController(
            pool=pool, emitter=emitter, notify_bridge=notifier,
        ).cancel(job)
        assert result["ok"] is True
        assert result["message"] == "取消已请求，等待后台停止确认"
        assert result["detail"]["finalized"] is False
        assert result["detail"]["c2_attempts"] == 0
        assert transport.frames[-1][0].phase == "canceling"
        assert not any(final for _, final in transport.frames)
        assert notifier.calls == []
        assert pool.receipts() == []
    finally:
        os.close(fd)


def test_unclaimed_cancel_can_finish_without_launching(tmp_path):
    pool = Pool(tmp_path / "pool")
    job = pool.submit("unclaimed-cancel", {"goal": "work"})
    assert pool.finalize_cancel(job) is True
    assert pool.status(job)["status"] == "cancelled"
    assert len(pool.receipts()) == 1


def test_unclaimed_control_cancel_still_emits_terminal_once(tmp_path):
    pool = Pool(tmp_path / "pool")
    job = pool.submit("queued-control", {"goal": "work"})
    cards = CardStore(tmp_path / "cards.db")
    cards.bind(job, "card-1", "message-1")
    transport = RecordingTransport()
    emitter = CardEmitter(cards, job, transport)
    emitter.open(phase="queued")
    notifier = RecordingNotifier()
    controller = BargeInController(pool=pool, emitter=emitter, notify_bridge=notifier)
    result = controller.cancel(job)
    assert result["detail"]["finalized"] is True
    assert result["detail"]["c2_attempts"] == 1
    assert transport.frames[-1][1] is True
    assert transport.frames[-1][0].state == "cancelled"
    assert len(notifier.calls) == 1
    controller.cancel(job)
    assert len(notifier.calls) == 1
    assert sum(final for _, final in transport.frames) == 1
    assert pool.finalize_cancel(job) is False
    assert pool.claim() is None
    assert len(pool.receipts()) == 1


def test_parked_attempt_still_needs_stop_confirmation(tmp_path):
    pool = Pool(tmp_path / "pool")
    job = pool.submit("parked-cancel", {"goal": "work"})
    _, fd = pool.claim()
    try:
        assert pool.pause(job)
        assert pool.finalize_cancel(job)
        assert pool.status(job)["status"] == "waiting_user"
        assert pool.status(job)["stage"] == "canceling"
        assert pool.receipts() == []
    finally:
        os.close(fd)


def test_cancel_control_does_not_relabel_completed_or_missing_task(tmp_path):
    pool = Pool(tmp_path / "pool")
    job = pool.submit("completed-cancel", {"goal": "work"})
    row, fd = pool.claim()
    try:
        assert pool.finish(job, row["attempt"], True, {"summary": "done"})
    finally:
        os.close(fd)
    notifier = RecordingNotifier()
    controller = BargeInController(pool=pool, notify_bridge=notifier)
    result = controller.cancel(job)
    assert result["ok"] is False
    assert result["detail"]["finalized"] is False
    assert pool.status(job)["status"] == "completed"
    assert notifier.calls == []
    missing = controller.cancel("missing")
    assert missing["ok"] is False
    assert missing["detail"]["finalized"] is False
    assert notifier.calls == []


def _alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


@pytest.mark.parametrize('outcome', ['cancel', 'timeout', 'completed'])
def test_terminal_reaps_detached_environment_clearing_descendant(tmp_path, outcome):
    pool = Pool(tmp_path / "pool")
    job = pool.submit("escaped-child", {})
    child_file = tmp_path / "escaped.pid"
    child_code = (
        "import os; os.setsid(); "
        "os.execve('/bin/sleep', ['sleep', '60'], {})"
    )
    code = (
        "import subprocess,sys,time; from pathlib import Path; "
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}], close_fds=True); "
        f"Path({str(child_file)!r}).write_text(str(p.pid)); "
        + ("time.sleep(.8); print('{\"ok\":true}')" if outcome == 'completed' else "time.sleep(60)")
    )
    child_pid = None
    with ThreadPoolExecutor(1) as executor:
        work = executor.submit(pool.run_once, [sys.executable, "-c", code],
                               1.5 if outcome == 'timeout' else 10, .2)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if child_file.exists():
                    child_pid = int(child_file.read_text())
                    if Path(f"/proc/{child_pid}/cmdline").read_bytes().startswith(b"sleep\0"):
                        break
                time.sleep(.01)
            else:
                pytest.fail("owned test child did not start")
            if outcome == 'cancel':
                pool.cancel(job)
            assert work.result(timeout=8)
            assert not _alive(child_pid), "terminal cancellation left an untagged detached child alive"
            assert pool.status(job)["status"] == {
                'cancel': 'cancelled', 'timeout': 'failed', 'completed': 'completed'}[outcome]
            assert len(pool.receipts()) == 1
        finally:
            pool.cancel(job)
            if child_pid and _alive(child_pid):
                os.kill(child_pid, 9)
            work.result(timeout=12)


def test_expired_cancel_window_is_not_stop_evidence_for_legacy_attempt(tmp_path):
    pool = Pool(tmp_path / "pool")
    job = pool.submit("cancel-timeout-unknown", {})
    _, fd = pool.claim()
    try:
        pool.cancel(job)
        # A legacy launch gap has no durable admission/stop record.
        with pool.db() as db:
            columns = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
            if "execution_state" in columns:
                db.execute("UPDATE jobs SET execution_state=NULL WHERE id=?", (job,))
        pool.enforce_windows(now=time.time() + 120, cancel_window_s=1)
        assert pool.status(job)["status"] == "running"
        assert pool.status(job)["stage"] == "canceling"
        assert pool.receipts() == []
        os.close(fd)
        fd = None
        assert pool.claim(lease=0) is None
        assert pool.status(job)["status"] == "running"
        assert pool.receipts() == []
    finally:
        if fd is not None:
            os.close(fd)


def test_finish_with_explicit_survivors_does_not_terminal_or_replay(tmp_path):
    pool = Pool(tmp_path / "pool")
    job = pool.submit("cancel-unquiesced", {}, replay_safe=True)
    row, fd = pool.claim()
    try:
        pool.cancel(job)
        assert pool.finish(job, row["attempt"], False, {"attempt_quiescent": False})
        assert pool.status(job)["status"] == "running"
        assert pool.status(job)["stage"] == "canceling"
        assert pool.receipts() == []
        assert pool.claim(lease=0) is None
    finally:
        os.close(fd)


def _wait(check, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(.01)
    pytest.fail("owned test process did not settle")


def test_worker_crash_supervisor_stops_environment_clearing_child(tmp_path):
    pool = Pool(tmp_path / "pool")
    job = pool.submit("crash-stop-tree", {}, replay_safe=True)
    child_file = tmp_path / "child.pid"
    code = (
        "import subprocess,sys,time; from pathlib import Path; "
        "p=subprocess.Popen([sys.executable,'-c',"
        "\"import os; os.setsid(); os.execve('/bin/sleep',['sleep','60'],{})\"],close_fds=True); "
        f"Path({str(child_file)!r}).write_text(str(p.pid)); time.sleep(60)"
    )
    worker_code = (
        "import sys; from agentcore.pool import Pool; "
        "Pool(sys.argv[1]).run_once([sys.executable,'-c',sys.argv[2]],timeout=20,lease=.2)"
    )
    worker = subprocess.Popen([sys.executable, "-c", worker_code, str(pool.root), code])
    child_pid = None
    try:
        _wait(child_file.exists)
        child_pid = int(child_file.read_text())
        _wait(lambda: Path(f"/proc/{child_pid}/cmdline").read_bytes().startswith(b"sleep\0"))
        first = pool.status(job)["attempt"]
        worker.kill()
        worker.wait(timeout=5)
        _wait(lambda: not _alive(child_pid))
        proof = pool.root / "attempts" / first / "stop.json"
        _wait(lambda: proof.exists() and proof.stat().st_size > 0)
        claim = _wait(lambda: pool.claim(lease=0))
        try:
            assert claim[0]["attempt"] != first
            assert pool.finish(job, first, True, {}) is False
        finally:
            os.close(claim[1])
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)
        if child_pid and _alive(child_pid):
            os.kill(child_pid, 9)


def test_killed_supervisor_cannot_be_replaced_without_stop_proof(tmp_path):
    pool = Pool(tmp_path / "pool")
    job = pool.submit("supervisor-loss", {}, replay_safe=True)
    child_file = tmp_path / "child.pid"
    code = f"import os,time; from pathlib import Path; Path({str(child_file)!r}).write_text(str(os.getpid())); time.sleep(60)"
    child_pid = None
    with ThreadPoolExecutor(1) as executor:
        work = executor.submit(pool.run_once, [sys.executable, "-c", code], 10, .2)
        try:
            _wait(child_file.exists)
            child_pid = int(child_file.read_text())
            os.kill(pool.status(job)["pid"], 9)
            assert work.result(timeout=8)
            assert _alive(child_pid)
            assert pool.status(job)["status"] == "running"
            assert pool.status(job)["execution_state"] == "supervised"
            assert pool.status(job)["stage"] == "stop_unconfirmed"
            assert pool.receipts() == []
            # Caller booleans are not allowed to replace the missing private receipt.
            assert pool.finish(job, pool.status(job)["attempt"], True, {"attempt_quiescent": True})
            pool.cancel(job)
            pool.enforce_windows(now=time.time() + 120, cancel_window_s=1)
            assert pool.claim(lease=0) is None
            pool.reconcile_watchdog(now=time.time() + 120, lease=1)
            assert pool.status(job)["status"] == "running"
            assert pool.status(job)["stage"] == "canceling"
            assert pool.receipts() == []
        finally:
            if child_pid and _alive(child_pid):
                os.kill(child_pid, 9)
            work.result(timeout=12)


@pytest.mark.parametrize("field", ["job", "attempt", "token", "nonce", "supervisor_pid", "supervisor_start", "proof_scope", "quiescent"])
def test_stop_receipt_binding_rejects_tampering(tmp_path, supervise_without_finish, field):
    pool = Pool(tmp_path / "pool")
    job = pool.submit("proof-binding", {}, replay_safe=True)
    claim = pool.claim()
    supervise_without_finish(pool, claim)
    proof_path = pool.root / "attempts" / claim[0]["attempt"] / "stop.json"
    proof = json.loads(proof_path.read_text())
    proof[field] = False if field == "quiescent" else "mismatch"
    proof_path.write_text(json.dumps(proof))
    assert pool.claim(lease=0) is None
    assert pool.status(job)["status"] == "running"
    assert pool.receipts() == []


def test_starting_timeout_fences_late_runner_launch(tmp_path):
    pool = Pool(tmp_path / "pool")
    job = pool.submit("launch-fence", {})
    claim = pool.claim()
    marker = tmp_path / "must-not-launch"
    pool.enforce_windows(now=time.time() + 120, start_window_s=1)
    assert pool.status(job)["status"] == "failed"
    pool.run_claimed(claim, [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"])
    assert not marker.exists()
    assert len(pool.receipts()) == 1


@pytest.mark.parametrize('control_action', ['cancel', 'pause'])
def test_slow_supervisor_handshake_does_not_hold_dispatch_lock(tmp_path, monkeypatch, control_action):
    import socket

    pool = Pool(tmp_path / 'pool')
    job = pool.submit('slow-handshake', {})
    marker = tmp_path / 'must-not-start'
    waiting, release = threading.Event(), threading.Event()
    original_recv = socket.socket.recv

    def delayed_ready(sock, *args, **kwargs):
        waiting.set()
        assert release.wait(5), 'test did not release the startup handshake'
        return original_recv(sock, *args, **kwargs)

    monkeypatch.setattr(socket.socket, 'recv', delayed_ready)
    with ThreadPoolExecutor(2) as executor:
        work = executor.submit(pool.run_once, [sys.executable, '-c',
                               f'from pathlib import Path; Path({str(marker)!r}).touch()'], 10, .2)
        try:
            assert waiting.wait(5), 'supervisor handshake did not start'

            def interact():
                assert pool.status(job)['stage'] == 'starting'
                assert pool.submit('second-message', {'goal': 'another task'})
                getattr(pool, control_action)(job)

            interaction = executor.submit(interact)
            try:
                interaction.result(timeout=1)
            except TimeoutError:
                pytest.fail('background startup blocked foreground dispatch/control')
        finally:
            release.set()
            assert work.result(timeout=12)
        assert not marker.exists(), 'a revoked startup still executed its runner'
        current = pool.status(job)
        assert current['status'] == ('cancelled' if control_action == 'cancel' else 'waiting_user')
        assert current['execution_state'] == 'supervised'
        with pool.db() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone()
        assert pool._attempt_stopped(row)
        assert len(pool.receipts()) == (1 if control_action == 'cancel' else 0)
        if control_action == 'pause':
            assert pool.resume_from_user(job)


def test_pause_cannot_resume_or_deny_to_terminal_before_executor_stops(tmp_path):
    pool = Pool(tmp_path / "pool")
    job = pool.submit("pause-fence", {})
    row, fd = pool.claim()
    try:
        assert pool.begin_execution(job, row["attempt"])
        assert pool.pause(job)
        assert pool.resume_from_user(job) is False
        assert pool.deny_from_user(job, reason="cancel this")
        assert pool.status(job)["status"] == "waiting_user"
        assert pool.status(job)["stage"] == "canceling"
        assert pool.receipts() == []
        assert pool.finish(job, row["attempt"], False, {"reason": "synchronous owner returned"})
        assert pool.status(job)["status"] == "cancelled"
        assert len(pool.receipts()) == 1
    finally:
        os.close(fd)


def test_unknown_stop_is_not_displayed_as_healthy_running(tmp_path):
    pool = Pool(tmp_path / "pool")
    job = pool.submit("unknown-stop-display", {})
    row, fd = pool.claim()
    try:
        assert pool.finish(job, row['attempt'], False, {'attempt_quiescent': False})
        status = pool.status(job)
        assert status['stage'] == 'stop_unconfirmed'
        cards = CardStore(tmp_path / "cards.db")
        cards.bind(job, "card-1", "message-1")
        transport = RecordingTransport()
        emitter = CardEmitter(cards, job, transport)
        emitter.open(phase="work")
        frame = emitter.apply_pool_stage(status['stage'])
        assert "停止尚未确认" in frame.note
        assert "运行中" not in frame.note
        assert frame.final is False
    finally:
        os.close(fd)
