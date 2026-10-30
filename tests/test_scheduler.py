import concurrent.futures
import gc
import importlib.util
import threading
import weakref

import pytest

from agentcore.scheduler import AgentScheduler


@pytest.fixture(params=['stdlib', 'flowcoro'])
def backend(request):
    if request.param == 'flowcoro':
        pytest.importorskip('flowcoro_py', reason='optional native binding is not installed')
    return request.param


def test_results_exceptions_and_no_retained_handles(backend):
    with AgentScheduler(backend, workers=2, capacity=4) as scheduler:
        handles = [scheduler.submit(str(n), lambda n=n: n) for n in range(20)]
        assert scheduler.gather(handles) == list(range(20))
        assert scheduler.wait_first(handles) in range(20)
        assert scheduler.pending_count == 0
        ref = weakref.ref(handles[0])
        del handles
        gc.collect()
        assert ref() is None


def test_backpressure_timeout_and_release(backend):
    release = threading.Event()
    entered = threading.Event()
    with AgentScheduler(backend, workers=1, capacity=1) as scheduler:
        def blocking():
            entered.set()
            assert release.wait(5)
            return 'first'
        handle = scheduler.submit('blocked', blocking)
        assert entered.wait(5)
        try:
            assert not handle.future.cancel()
            with pytest.raises(TimeoutError):
                scheduler.submit('overflow', lambda: None, admission_timeout=0)
            assert scheduler.wait_first([handle], timeout=0) is None
            with pytest.raises(TimeoutError):
                scheduler.gather([handle], timeout=0)
        finally:
            release.set()
        assert handle.result(5) == 'first'
        assert scheduler.submit('next', lambda: 7).result(5) == 7


def test_cooperative_and_ignored_cancel(backend):
    entered = threading.Event()
    with AgentScheduler(backend, workers=1) as scheduler:
        def cooperative(cancel_token, cancel_event):
            entered.set()
            assert cancel_event.wait(5)
            cancel_token.throw_if_cancellation_requested()
        handle = scheduler.submit('cooperative', cooperative)
        assert entered.wait(5)
        assert handle.cancel()
        with pytest.raises(Exception):
            handle.result(5)
        assert handle.cancelled()
        start = threading.Event()
        finish = threading.Event()
        def ignores():
            start.set()
            assert finish.wait(5)
            return 'successful despite request'
        ignored = scheduler.submit('ignores', ignores)
        assert start.wait(5)
        ignored.cancel()
        finish.set()
        assert ignored.result(5) == 'successful despite request'
        assert ignored.cancellation_requested() and not ignored.cancelled()


def test_gather_waits_all_before_raising_original_exception(backend):
    error = ValueError('original failure')
    finished = threading.Event()
    release = threading.Event()
    with AgentScheduler(backend, workers=2) as scheduler:
        def fail():
            raise error
        def complete():
            assert release.wait(5)
            finished.set()
            return 2
        handles = [scheduler.submit('bad', fail), scheduler.submit('good', complete)]
        release.set()
        with pytest.raises(ValueError) as caught:
            scheduler.gather(handles)
        assert caught.value is error and finished.is_set()


def test_close_unblocks_admission_and_drains(backend):
    release = threading.Event()
    scheduler = AgentScheduler(backend, workers=1, capacity=1)
    handle = scheduler.submit('blocking', lambda: release.wait(5))
    producer_entered = threading.Event()
    with concurrent.futures.ThreadPoolExecutor(2) as executor:
        def produce():
            producer_entered.set()
            return scheduler.submit('late', lambda: 1)
        producer = executor.submit(produce)
        assert producer_entered.wait(5)
        closer = executor.submit(scheduler.close)
        try:
            with pytest.raises(RuntimeError):
                producer.result(5)
        finally:
            release.set()
        closer.result(5)
    assert handle.done() and scheduler.closed and scheduler.pending_count == 0
    scheduler.close()
    with pytest.raises(RuntimeError):
        scheduler.submit('closed', lambda: 1)


def test_reject_cross_pool_and_self_close(backend):
    with AgentScheduler(backend) as first, AgentScheduler(backend) as second:
        handle = first.submit('self-close', first.close)
        with pytest.raises(RuntimeError, match='own scheduler'):
            handle.result(5)
        with pytest.raises(ValueError):
            second.wait_first([handle])
        assert not first.closed
        assert first.gather([]) == [] and first.wait_first([]) is None


def test_backend_is_explicit_and_missing_native_is_not_faked():
    with pytest.raises(ValueError):
        AgentScheduler('unknown')
    if importlib.util.find_spec('flowcoro_py') is None:
        with pytest.raises(ImportError):
            AgentScheduler('flowcoro')


def test_token_positional_argument_not_overwritten():
    with AgentScheduler() as scheduler:
        assert scheduler.submit('positional', lambda cancel_token: cancel_token, 'provided').result() == 'provided'


def test_worker_concurrency_and_bounded_queue(backend):
    ready = threading.Semaphore(0)
    release = threading.Event()
    with AgentScheduler(backend, workers=2, capacity=3) as scheduler:
        def blocked():
            ready.release()
            assert release.wait(5)
        handles = [scheduler.submit(str(n), blocked) for n in range(3)]
        try:
            assert ready.acquire(timeout=5) and ready.acquire(timeout=5)
            assert not ready.acquire(blocking=False)
            assert scheduler.pending_count == 3
            with pytest.raises(TimeoutError):
                scheduler.submit('full', blocked, admission_timeout=0)
        finally:
            release.set()
        scheduler.gather(handles)
        assert scheduler.pending_count == 0


def test_close_cooperatively_cancels_queued_work(backend):
    started = threading.Event()
    scheduler = AgentScheduler(backend, workers=1, capacity=2)
    def tool(cancel_event, cancel_token):
        started.set()
        assert cancel_event.wait(5)
        cancel_token.throw_if_cancellation_requested()
    running = scheduler.submit('running', tool)
    assert started.wait(5)
    queued = scheduler.submit('queued', tool)
    scheduler.close(cancel_pending=True)
    assert running.cancelled() and queued.cancelled()
    assert scheduler.pending_count == 0
