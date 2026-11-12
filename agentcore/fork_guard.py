"""Process-wide registration of flock FDs that must not survive ``fork()``.

A forked child inherits every open file description, including advisory locks.
A child that keeps the parent's guard FD alive pins the parent's lock after the
parent leaves the critical section, and its own lock attempt can then deadlock.

Registration of an FD and the fork itself must be indivisible: a fork in
another thread between ``os.open`` and ``track`` would inherit an untracked
FD. The lock is therefore held across both operations, and the at-fork hooks
acquire/release it around the fork.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
import threading
from typing import Iterator

_FDS: set = set()
_LOCK = threading.RLock()


def _fork_prepare() -> None:
    _LOCK.acquire()


def _fork_parent() -> None:
    _LOCK.release()


def _fork_child() -> None:
    try:
        for fd in tuple(_FDS):
            try:
                os.close(fd)
            except OSError:
                pass  # The child has no usable inherited guard in either case.
        _FDS.clear()
    finally:
        _LOCK.release()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(before=_fork_prepare, after_in_parent=_fork_parent, after_in_child=_fork_child)


@contextmanager
def hold() -> Iterator[None]:
    """Hold the registration lock across a caller-managed open/track sequence."""
    with _LOCK:
        yield


def track(fd: int) -> int:
    """Register an already-open FD; prefer :func:`open_tracked` when possible."""
    with _LOCK:
        _FDS.add(fd)
    return fd


def untrack(fd: int) -> bool:
    """Unregister ``fd``; return True when this process still owns it.

    A forked child clears the whole set, so untracking an inherited number
    returns False and the caller must not close a possibly reused descriptor.
    """
    with _LOCK:
        if fd in _FDS:
            _FDS.discard(fd)
            return True
        return False


def open_tracked(path, flags: int, mode: int = 0o600) -> int:
    """Open ``path`` and register the FD atomically with respect to ``fork()``."""
    with _LOCK:
        fd = os.open(path, flags, mode)
        _FDS.add(fd)
    return fd
