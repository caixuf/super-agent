"""Attempt containment evidence for colleague execution.

A process group only fences children that stay in the group. Hermes may start
detached terminal processes (``setsid``, ``systemd-run --scope``), which leave
the group and survive a ``killpg``. Every attempt therefore carries a random
token in its environment: the environment is inherited across fork/exec, so
scanning ``/proc/<pid>/environ`` finds those escaped descendants and gives the
caller evidence that the attempt is gone before a work reservation is settled.

This is evidence, not a sandbox. A descendant that clears its own environment
(or belongs to another user, so its ``environ`` is unreadable) is invisible to
the scan; the reservation then stays open for host reconciliation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import time
import uuid

ENV = "AGENTCORE_ATTEMPT_TOKEN"
_TOKEN = re.compile(r"[0-9a-f]{32}\Z")


def new_token() -> str:
    """Mint one attempt token; 32 hex characters, safe to place in an environment."""
    return uuid.uuid4().hex


def checked_token(token) -> str:
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise ValueError("invalid_attempt_token")
    return token


def _tagged(token: str, exclude: frozenset) -> list[int]:
    needle = ("%s=%s" % (ENV, token)).encode("ascii")
    procfd = os.open("/proc", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        names = os.listdir(procfd)
    finally:
        os.close(procfd)
    found = []
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid in exclude:
            continue
        try:
            fd = os.open("/proc/%d/environ" % pid, os.O_RDONLY)
        except OSError:
            continue
        try:
            with os.fdopen(fd, "rb") as stream:
                blob = stream.read(1 << 20)
        except OSError:
            continue
        # Entries are NUL separated and may be in any order.
        if needle in blob.split(b"\0"):
            found.append(pid)
    return sorted(found)


def tagged(token: str, *, exclude: frozenset = frozenset()) -> list[int]:
    """PIDs currently carrying the attempt token, excluding ``exclude`` and the caller."""
    token = checked_token(token)
    return _tagged(token, frozenset(exclude) | {os.getpid()})


def _signal(pid: int, signum: int) -> None:
    try:
        os.kill(pid, signum)
    except (ProcessLookupError, PermissionError):
        pass


def sweep(token: str, *, grace: float = 2.0, timeout: float = 10.0) -> dict:
    """Terminate every process still carrying ``token`` and prove the attempt quiesced.

    Returns durable evidence: which PIDs were signalled, which survived, and
    whether the attempt is now empty. ``survivors`` non-empty means the attempt
    did not quiesce and its reservation must not be settled.
    """
    token = checked_token(token)
    me = frozenset({os.getpid()})
    signalled: list[int] = []
    pending = _tagged(token, me)
    if pending:
        for pid in pending:
            _signal(pid, signal.SIGTERM)
        signalled = pending
        end = time.monotonic() + grace
        while time.monotonic() < end:
            pending = _tagged(token, me)
            if not pending:
                break
            time.sleep(0.05)
        for pid in pending:
            _signal(pid, signal.SIGKILL)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            pending = _tagged(token, me)
            if not pending:
                break
            time.sleep(0.05)
    return {"token": token, "signalled": signalled, "survivors": sorted(pending),
            "quiescent": not pending, "observed_at": time.time()}


def evidence_id(evidence) -> str | None:
    """Stable identity for one quiescence observation, or None when it is not evidence."""
    if not isinstance(evidence, dict) or evidence.get("quiescent") is not True:
        return None
    try:
        token = checked_token(evidence.get("token"))
    except ValueError:
        return None
    payload = json.dumps([token, sorted(evidence.get("survivors") or []),
                          list(evidence.get("signalled") or [])], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:32]
