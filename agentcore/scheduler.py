"""Bounded same-process tool fanout, optionally backed by flowcoro_py.

Durability belongs to the job store, not this pool. Cancellation requests are
cooperative: a tool ignoring its token may still return successfully. No thread
is killed. Native flowcoro scheduling is opt-in and never silently substituted.
"""
from __future__ import annotations

import concurrent.futures as futures
import importlib
import inspect
import threading
import time


class CancellationToken:
    def __init__(self, event):
        self._event = event

    def is_cancelled(self):
        return self._event.is_set()

    def throw_if_cancellation_requested(self):
        if self.is_cancelled():
            raise futures.CancelledError('cooperative cancellation requested')


class ToolHandle:
    def __init__(self, name, owner, source=None):
        self.name = name
        self.future = futures.Future()
        # The bridge is observational. Even queued native work cannot be
        # preempted, so raw Future.cancel() must not free admission early.
        self.future.set_running_or_notify_cancel()
        self.cancel_event = threading.Event()
        self.source = source
        self.cancel_token = source.token() if source else CancellationToken(self.cancel_event)
        self._owner = owner
        self._completed_order = None

    def done(self):
        return self.future.done()

    def result(self, timeout=None):
        return self.future.result(timeout=timeout)

    def exception(self, timeout=None):
        return self.future.exception(timeout=timeout)

    def cancellation_requested(self):
        return self.cancel_event.is_set()

    def cancelled(self):
        """True only for a cooperative cancellation exception, not a request."""
        if not self.done():
            return False
        return isinstance(self.future.exception(), self._owner._cancel_errors)

    def cancel(self):
        return self._owner.cancel(self)


def _accepts_keyword(fn, name):
    try:
        parameters = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    param = parameters.get(name)
    return bool(param and param.kind in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY)) or any(
        p.kind == p.VAR_KEYWORD for p in parameters.values())


class AgentScheduler:
    def __init__(self, backend='stdlib', workers=4, capacity=16):
        if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
            raise ValueError('workers must be a positive integer')
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError('capacity must be a positive integer')
        self.backend = backend
        self.capacity = capacity
        self._cv = threading.Condition()
        self._close_lock = threading.Lock()
        self._local = threading.local()
        self._active = set()
        self._closed = False
        self._sequence = 0
        self._fc = None
        self._cancel_errors = (futures.CancelledError,)
        if backend == 'stdlib':
            self._pool = futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix='agent-tool')
        elif backend == 'flowcoro':
            self._fc = importlib.import_module('flowcoro_py')
            fc_cancel = getattr(self._fc, 'CancelledError', None)
            if fc_cancel is not None:
                self._cancel_errors += (fc_cancel,)
            self._pool = self._fc.CoroutineThreadPool(threads=workers, pin_to_cores=False)
        else:
            raise ValueError('backend must be stdlib or flowcoro')

    @property
    def pending_count(self):
        with self._cv:
            return len(self._active)

    @property
    def closed(self):
        with self._cv:
            return self._closed

    def _complete(self, handle):
        with self._cv:
            self._active.discard(handle)
            self._cv.notify_all()

    def submit(self, name, fn, *args, admission_timeout=None, **kwargs):
        if admission_timeout is not None and admission_timeout < 0:
            raise ValueError('admission_timeout must be nonnegative')
        deadline = None if admission_timeout is None else time.monotonic() + admission_timeout
        with self._cv:
            while len(self._active) >= self.capacity and not self._closed:
                if getattr(self._local, 'inside', False):
                    raise RuntimeError('nested submission would block its own scheduler')
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError('scheduler capacity exhausted')
                self._cv.wait(remaining)
            if self._closed:
                raise RuntimeError('scheduler is closed')
            source = self._fc.CancellationTokenSource() if (self._fc and hasattr(self._fc, 'CancellationTokenSource')) else None
            handle = ToolHandle(name, self, source)
            inject = dict(kwargs)
            # Bind supplied positional arguments first, avoiding duplicate injection.
            try:
                supplied = inspect.signature(fn).bind_partial(*args, **kwargs).arguments
            except (TypeError, ValueError):
                supplied = kwargs
            for key, value in (('cancel_token', handle.cancel_token), ('cancel_event', handle.cancel_event)):
                if key not in supplied and key not in inject and _accepts_keyword(fn, key):
                    inject[key] = value
            self._active.add(handle)
            handle.future.add_done_callback(lambda unused: self._complete(handle))

            def execute():
                self._local.inside = True
                try:
                    value = fn(*args, **inject)
                except BaseException as error:
                    with self._cv:
                        self._sequence += 1
                        handle._completed_order = self._sequence
                        handle.future.set_exception(error)
                else:
                    with self._cv:
                        self._sequence += 1
                        handle._completed_order = self._sequence
                        handle.future.set_result(value)
                finally:
                    self._local.inside = False

            try:
                # Submit under the admission lock so close cannot overtake it.
                self._pool.submit(execute)
            except BaseException as error:
                handle.future.set_exception(error)
                raise
            return handle

    submit_tool = submit

    def _validate(self, handles):
        result = list(handles)
        if any(not isinstance(h, ToolHandle) or h._owner is not self for h in result):
            raise ValueError('all handles must belong to this scheduler')
        return result

    def gather(self, handles, timeout=None):
        handles = self._validate(handles)
        if not handles:
            return []
        _, pending = futures.wait([h.future for h in handles], timeout=timeout)
        if pending:
            raise TimeoutError('gather timed out; tools continue running')
        # Wait for all, then preserve input order and the original exception.
        return [h.result() for h in handles]

    def wait_first(self, handles, timeout=None):
        handles = self._validate(handles)
        if not handles:
            return None
        done, _ = futures.wait([h.future for h in handles], timeout=timeout,
                               return_when=futures.FIRST_COMPLETED)
        if not done:
            return None
        return min((i for i, h in enumerate(handles) if h.future in done),
                   key=lambda i: handles[i]._completed_order)

    def cancel(self, handle):
        self._validate([handle])
        with self._cv:
            if handle.done():
                return False
            handle.cancel_event.set()
            if handle.source:
                handle.source.cancel()
            return True

    def cancel_all(self):
        with self._cv:
            for handle in tuple(self._active):
                self.cancel(handle)

    def close(self, cancel_pending=False):
        if getattr(self._local, 'inside', False):
            raise RuntimeError('a tool cannot close its own scheduler')
        with self._close_lock:
            with self._cv:
                self._closed = True
                self._cv.notify_all()
                if cancel_pending:
                    self.cancel_all()
            # Both backends drain queued work. Ignored cancellation can delay close.
            if self._fc:
                self._pool.close()
            else:
                self._pool.shutdown(wait=True)

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


Scheduler = AgentScheduler
