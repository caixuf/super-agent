"""Host-only colleague instance lifecycle, independent of any particular runtime.

The injected backend is trusted infrastructure, not a model tool. It MUST use
stable (profile_id, member_id, generation) identity, be idempotent across
crashes, enforce its own isolation, and never copy messaging credentials.
Every job admission must reserve registry work before entering any external
queue; all producers must obey the same fence. Until that integration exists,
this module cannot safely retire real workers.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import threading
import time

from agentcore.colleague_registry import Supervisor


# A forked backend must not keep the parent's flock open. Synchronize fork
# with FD registration, so a fork in another thread cannot miss a new FD.
_guard_fds = set()
_guard_fds_lock = threading.Lock()

def _fork_prepare():
    _guard_fds_lock.acquire()

def _fork_parent():
    _guard_fds_lock.release()

def _fork_child():
    try:
        for fd in tuple(_guard_fds):
            try:
                os.close(fd)
            except OSError:
                pass
        _guard_fds.clear()
    finally:
        _guard_fds_lock.release()

if hasattr(os, 'register_at_fork'):
    os.register_at_fork(before=_fork_prepare, after_in_parent=_fork_parent, after_in_child=_fork_child)


@dataclass(frozen=True)
class InstanceSpec:
    """Trusted identity plus descriptive duties; no executable or storage paths."""
    profile_id: str
    member_id: str
    generation: int
    role_id: str
    display_name: str
    duty: str
    boundaries: tuple[str, ...]


class Provisioner:
    """Reconcile registered members with a host-injected instance backend.

    backend.ensure(spec) and backend.park(spec) must be idempotent for spec's
    identity, including when an earlier call performed its effect then raised.
    backend.inspect(spec) must return a mapping with state, and explicit boolean
    attestations isolated/private_memory/no_channels when state is 'ready'.
    backend.idle(spec) is a host-authoritative observation of *all* external
    work, including pending delivery. It must return exactly True to progress.
    Backends must fence external admission while idle/park is being reconciled.
    This object is a privileged host interface, not an authentication boundary.
    """

    def __init__(self, registry, *, supervisor, backend, state_dir, lock_timeout=5.0):
        if type(supervisor) is not Supervisor or supervisor.profile_id != registry.profile_id:
            raise PermissionError('trusted_supervisor_required')
        if isinstance(lock_timeout, bool) or not isinstance(lock_timeout, (int, float)) or not math.isfinite(lock_timeout) or lock_timeout < 0:
            raise ValueError('invalid_lock_timeout')
        self.lock_timeout = lock_timeout
        self.registry, self.supervisor, self.backend = registry, supervisor, backend
        self.state_dir = Path(state_dir)
        if not self.state_dir.is_absolute() or self.state_dir.is_symlink() or not self.state_dir.is_dir():
            raise ValueError('private_state_directory_required')
        info = self.state_dir.stat()
        if info.st_mode & 0o077 or (hasattr(os, 'getuid') and info.st_uid != os.getuid()):
            raise ValueError('private_state_directory_required')

    def _spec(self, member_id, generation):
        info = self.registry.role_for_member(supervisor=self.supervisor, member_id=member_id, generation=generation)
        primary = self.registry.primary_member(supervisor=self.supervisor)
        if member_id == primary or info['role_id'] == 'primary':
            raise PermissionError('primary_protected')
        return InstanceSpec(self.registry.profile_id, member_id, generation, info['role_id'],
                            info['display_name'], info['duty'], tuple(info['boundaries'])), info['status']

    def _name(self, spec):
        raw = json.dumps([spec.profile_id, spec.member_id, spec.generation], separators=(',', ':'))
        return hashlib.sha256(raw.encode()).hexdigest()

    @contextmanager
    def _guard(self, spec):
        path = self.state_dir / (self._name(spec) + '.lock')
        flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
        with _guard_fds_lock:
            fd = os.open(path, flags, 0o600)
            _guard_fds.add(fd)
        owner_pid = os.getpid()
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1
                    or (hasattr(os, 'getuid') and info.st_uid != os.getuid())):
                raise ValueError('private_state_file_required')
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('member lifecycle guard busy') from None
                    time.sleep(min(remaining, 0.05))
            yield
        finally:
            # The child callback already closed this FD; it may have been reused.
            if os.getpid() == owner_pid:
                with _guard_fds_lock:
                    _guard_fds.discard(fd)
                    os.close(fd)

    def _save(self, spec, phase):
        record = {'profile_id': spec.profile_id, 'member_id': spec.member_id,
                  'generation': spec.generation, 'phase': phase}
        fd, tmp = tempfile.mkstemp(dir=self.state_dir, prefix='.provisioner-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                json.dump(record, out, sort_keys=True)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, self.state_dir / (self._name(spec) + '.json'))
            directory = os.open(self.state_dir, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _phase(self, spec):
        path = self.state_dir / (self._name(spec) + '.json')
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
        except FileNotFoundError:
            return 'unstarted'
        with os.fdopen(fd, 'r', encoding='utf-8') as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 4096
                    or (hasattr(os, 'getuid') and info.st_uid != os.getuid())):
                raise ValueError('private_state_file_required')
            record = json.load(source)
        if not isinstance(record, dict):
            raise ValueError('invalid_lifecycle_record')
        if (record.get('profile_id'), record.get('member_id'), record.get('generation')) != (spec.profile_id, spec.member_id, spec.generation):
            raise ValueError('invalid_lifecycle_record')
        if record.get('phase') not in {'starting', 'ready', 'draining', 'parking', 'parked'}:
            raise ValueError('invalid_lifecycle_record')
        return record['phase']

    def _inspection(self, spec):
        result = self.backend.inspect(spec)
        if not isinstance(result, dict) or result.get('state') not in {'absent', 'starting', 'ready', 'parked', 'unknown'}:
            raise ValueError('invalid_backend_observation')
        if result['state'] == 'ready' and any(result.get(k) is not True for k in ('isolated', 'private_memory', 'no_channels')):
            raise ValueError('unsafe_backend_observation')
        return result['state']

    def status(self, member_id, generation):
        spec, _ = self._spec(member_id, generation)
        with self._guard(spec):
            return self._status_locked(spec)

    def provision(self, member_id, generation):
        spec, _ = self._spec(member_id, generation)
        with self._guard(spec):
            spec, membership = self._spec(member_id, generation)
            if membership != 'active':
                raise ValueError('member_not_active')
            phase = self._phase(spec)
            if phase in {'draining', 'parking', 'parked'}:
                raise ValueError('lifecycle_not_provisionable')
            if phase == 'ready' and self._inspection(spec) == 'ready':
                return self._status_locked(spec)
            self._save(spec, 'starting')
            self.registry.observe_runtime(supervisor=self.supervisor, member_id=member_id, generation=generation, state='starting')
            self.backend.ensure(spec)
            state = self._inspection(spec)
            # A manager can freeze the registry while the slow backend is starting.
            _, membership = self._spec(member_id, generation)
            if membership != 'active':
                self._save(spec, 'draining')
                return self._status_locked(spec)
            if state == 'ready':
                self._save(spec, 'ready')
                self.registry.observe_runtime(supervisor=self.supervisor, member_id=member_id, generation=generation, state='running')
            return self._status_locked(spec)

    def _status_locked(self, spec):
        phase = self._phase(spec)
        state = self._inspection(spec)
        # Read membership after potentially slow backend inspection. This is a
        # snapshot only; actual admission must use registry.reserve_work.
        _, membership = self._spec(spec.member_id, spec.generation)
        return {'member_id': spec.member_id, 'generation': spec.generation,
                'membership_status': membership, 'phase': phase, 'runtime_state': state,
                'ready': membership == 'active' and phase == 'ready' and state == 'ready'}

    def deactivate(self, *, actor, member_id, request_id):
        # Freeze immediately, even if a backend operation currently holds the guard.
        result = self.registry.request_deactivation(actor=actor, member_id=member_id, request_id=request_id)
        return self.reconcile_deactivation(member_id, result['generation'])

    def reconcile_deactivation(self, member_id, generation):
        spec, _ = self._spec(member_id, generation)
        with self._guard(spec):
            spec, membership = self._spec(member_id, generation)
            if membership == 'active':
                raise ValueError('member_not_draining')
            phase = self._phase(spec)
            if phase == 'parked' and membership == 'inactive' and self._inspection(spec) in {'parked', 'absent'}:
                return self._status_locked(spec)
            if membership == 'draining':
                self._save(spec, 'draining')
                work = self.registry.work_status(supervisor=self.supervisor, member_id=member_id, generation=generation)
                if work['tracked_open'] or self.backend.idle(spec) is not True:
                    return self._status_locked(spec)
                # This transaction rechecks the reservations after the idle observation.
                try:
                    self.registry.finalize_deactivation(supervisor=self.supervisor, member_id=member_id,
                                                        generation=generation, external_idle=True)
                except ValueError as exc:
                    if str(exc) != 'active_work_remaining':
                        raise
                    return self._status_locked(spec)
            self._save(spec, 'parking')
            # Re-check external activity even after a crash following finalization.
            if self.backend.idle(spec) is not True:
                return self._status_locked(spec)
            self.registry.observe_runtime(supervisor=self.supervisor, member_id=member_id, generation=generation, state='stopping')
            self.backend.park(spec)
            if self._inspection(spec) not in {'parked', 'absent'}:
                return self._status_locked(spec)
            self.registry.observe_runtime(supervisor=self.supervisor, member_id=member_id, generation=generation, state='stopped')
            self._save(spec, 'parked')
            return self._status_locked(spec)
