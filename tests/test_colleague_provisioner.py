from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from agentcore.colleague_registry import Actor, Registry, Supervisor
from agentcore.colleague_provisioner import Provisioner


class FakeBackend:
    def __init__(self):
        self.instances = {}
        self.idle_value = True
        self.ensure_calls = 0
        self.park_calls = 0
        self.fail_ensure = False
        self.fail_park = False
        self.unsafe = False
        self.entered = None
        self.release = None

    def key(self, spec):
        return spec.profile_id, spec.member_id, spec.generation

    def ensure(self, spec):
        self.ensure_calls += 1
        if self.entered:
            self.entered.set()
            assert self.release.wait(5)
        self.instances[self.key(spec)] = ('ready', spec)
        if self.fail_ensure:
            self.fail_ensure = False
            raise RuntimeError('lost ensure response')

    def inspect(self, spec):
        state, _ = self.instances.get(self.key(spec), ('absent', None))
        return {'state': state, 'isolated': not self.unsafe, 'private_memory': True, 'no_channels': True}

    def idle(self, spec):
        return self.idle_value

    def park(self, spec):
        self.park_calls += 1
        self.instances[self.key(spec)] = ('parked', spec)
        if self.fail_park:
            self.fail_park = False
            raise RuntimeError('lost park response')


@pytest.fixture
def context(tmp_path):
    registry = Registry(tmp_path / 'registry.sqlite', profile_id='team', primary_id='leader')
    actor, host = Actor('team', 'leader'), Supervisor('team')
    registry.define_role(actor=actor, role_id='analysis', label='Analyst', duty='Read evidence',
                         boundaries=['Do not modify'], request_id='role')
    registry.hire(actor=actor, member_id='alice', role_id='analysis', display_name='Alice', request_id='hire')
    state_dir = tmp_path / 'lifecycle'
    state_dir.mkdir(mode=0o700)
    backend = FakeBackend()
    supervisor = Provisioner(registry, supervisor=host, backend=backend, state_dir=state_dir)
    return registry, actor, host, backend, supervisor, state_dir


def test_provision_is_ready_only_after_backend_and_uses_registered_spec(context):
    registry, actor, host, backend, supervisor, directory = context
    assert supervisor.status('alice', 1)['ready'] is False
    ready = supervisor.provision('alice', 1)
    assert ready['ready'] is True and ready['runtime_state'] == 'ready'
    spec = backend.instances[('team', 'alice', 1)][1]
    assert (spec.role_id, spec.duty, spec.boundaries) == ('analysis', 'Read evidence', ('Do not modify',))
    assert not hasattr(spec, 'path') and not hasattr(spec, 'command') and not hasattr(spec, 'credentials')
    assert supervisor.provision('alice', 1)['ready'] and backend.ensure_calls == 1
    backend.instances[('team', 'alice', 1)] = ('unknown', spec)
    assert supervisor.status('alice', 1)['ready'] is False


def test_primary_invalid_generation_and_untrusted_host_are_rejected(context):
    registry, actor, host, backend, supervisor, directory = context
    for fn in (lambda: supervisor.provision('leader', 1), lambda: supervisor.status('leader', 1),
               lambda: supervisor.deactivate(actor=actor, member_id='leader', request_id='remove-primary'),
               lambda: supervisor.reconcile_deactivation('leader', 1)):
        with pytest.raises(PermissionError, match='primary_protected'):
            fn()
    with pytest.raises(ValueError, match='stale_member_generation'):
        supervisor.provision('alice', 2)
    with pytest.raises(PermissionError):
        Provisioner(registry, supervisor=Supervisor('other'), backend=backend, state_dir=directory)
    assert not backend.instances


def test_failure_after_create_replays_same_instance_and_unsafe_never_ready(context):
    registry, actor, host, backend, supervisor, directory = context
    backend.fail_ensure = True
    with pytest.raises(RuntimeError, match='lost ensure'):
        supervisor.provision('alice', 1)
    assert supervisor.status('alice', 1)['phase'] == 'starting'
    another = Provisioner(registry, supervisor=host, backend=backend, state_dir=directory)
    assert another.provision('alice', 1)['ready']
    assert len(backend.instances) == 1
    backend.unsafe = True
    with pytest.raises(ValueError, match='unsafe_backend_observation'):
        another.status('alice', 1)


def test_retirement_waits_for_reservation_and_external_idle_preserves_instance(context):
    registry, actor, host, backend, supervisor, directory = context
    supervisor.provision('alice', 1)
    registry.reserve_work(supervisor=host, member_id='alice', generation=1, work_id='job')
    assert supervisor.deactivate(actor=actor, member_id='alice', request_id='retire')['phase'] == 'draining'
    assert backend.park_calls == 0
    with pytest.raises(ValueError, match='member_not_accepting_work'):
        registry.reserve_work(supervisor=host, member_id='alice', generation=1, work_id='new')
    registry.settle_work(supervisor=host, member_id='alice', generation=1, work_id='job')
    backend.idle_value = False
    assert supervisor.reconcile_deactivation('alice', 1)['membership_status'] == 'draining'
    assert backend.park_calls == 0
    backend.idle_value = True
    status = supervisor.reconcile_deactivation('alice', 1)
    assert (status['membership_status'], status['phase'], status['ready']) == ('inactive', 'parked', False)
    assert len(backend.instances) == 1
    assert supervisor.reconcile_deactivation('alice', 1)['phase'] == 'parked'
    assert backend.park_calls == 1
    assert any(e['action'] == 'member_hired' for e in registry.history(actor=actor))


def test_park_lost_response_recovers_after_restart(context):
    registry, actor, host, backend, supervisor, directory = context
    supervisor.provision('alice', 1)
    backend.fail_park = True
    with pytest.raises(RuntimeError, match='lost park'):
        supervisor.deactivate(actor=actor, member_id='alice', request_id='retire')
    assert supervisor.status('alice', 1)['phase'] == 'parking'
    assert supervisor.status('alice', 1)['membership_status'] == 'inactive'
    new = Provisioner(registry, supervisor=host, backend=backend, state_dir=directory)
    assert new.reconcile_deactivation('alice', 1)['phase'] == 'parked'
    assert len(backend.instances) == 1


def test_concurrent_ensures_are_serialized(context):
    registry, actor, host, backend, supervisor, directory = context
    other = Provisioner(registry, supervisor=host, backend=backend, state_dir=directory)
    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(supervisor.provision, 'alice', 1)
        f2 = pool.submit(other.provision, 'alice', 1)
        assert f1.result(timeout=8)['ready'] and f2.result(timeout=8)['ready']
    assert backend.ensure_calls == 1


def test_freeze_during_slow_provision_does_not_report_ready(context):
    registry, actor, host, backend, supervisor, directory = context
    backend.entered, backend.release = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        creating = pool.submit(supervisor.provision, 'alice', 1)
        assert backend.entered.wait(5)
        retiring = pool.submit(supervisor.deactivate, actor=actor, member_id='alice', request_id='retire')
        for _ in range(200):
            if registry.role_for_member(supervisor=host, member_id='alice', generation=1)['status'] == 'draining':
                break
        else:
            pytest.fail('retirement did not freeze the registry')
        backend.release.set()
        assert creating.result(timeout=8)['ready'] is False
        assert retiring.result(timeout=8)['phase'] == 'parked'


def test_unknown_external_idle_never_allows_park(context):
    registry, actor, host, backend, supervisor, directory = context
    supervisor.provision('alice', 1)
    backend.idle_value = None
    result = supervisor.deactivate(actor=actor, member_id='alice', request_id='retire')
    assert result['phase'] == 'draining' and backend.park_calls == 0


def test_private_state_dir_is_required(context, tmp_path):
    registry, actor, host, backend, supervisor, directory = context
    directory.chmod(0o755)
    with pytest.raises(ValueError, match='private_state_directory_required'):
        Provisioner(registry, supervisor=host, backend=backend, state_dir=directory)


def test_guard_serializes_an_independent_process(context):
    import subprocess
    import sys

    registry, actor, host, backend, supervisor, directory = context
    spec, _ = supervisor._spec('alice', 1)
    script = """
import sys
from agentcore.colleague_registry import Registry, Supervisor
from agentcore.colleague_provisioner import Provisioner
r = Registry(sys.argv[1], profile_id='team', primary_id='leader')
p = Provisioner(r, supervisor=Supervisor('team'), backend=None, state_dir=sys.argv[2])
s, _ = p._spec('alice', 1)
print('waiting', flush=True)
with p._guard(s):
    print('acquired', flush=True)
"""
    with supervisor._guard(spec):
        child = subprocess.Popen([sys.executable, '-c', script, str(registry.path), str(directory)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert child.stdout.readline().strip() == 'waiting'
            with pytest.raises(subprocess.TimeoutExpired):
                child.wait(timeout=0.3)
        finally:
            pass
    stdout, stderr = child.communicate(timeout=5)
    assert child.returncode == 0, stderr
    assert stdout.strip() == 'acquired'


def test_restart_after_finalization_before_parking(context, monkeypatch):
    registry, actor, host, backend, supervisor, directory = context
    supervisor.provision('alice', 1)
    original = supervisor._save
    def fail_on_parking(spec, phase):
        if phase == 'parking':
            raise RuntimeError('simulated crash')
        return original(spec, phase)
    monkeypatch.setattr(supervisor, '_save', fail_on_parking)
    with pytest.raises(RuntimeError, match='simulated crash'):
        supervisor.deactivate(actor=actor, member_id='alice', request_id='retire')
    assert backend.park_calls == 0
    resumed = Provisioner(registry, supervisor=host, backend=backend, state_dir=directory)
    assert resumed.status('alice', 1)['membership_status'] == 'inactive'
    assert resumed.reconcile_deactivation('alice', 1)['phase'] == 'parked'


def test_distinct_members_have_isolated_instances_and_stop_independently(context):
    registry, actor, host, backend, supervisor, directory = context
    registry.hire(actor=actor, member_id='bob', role_id='analysis', display_name='Bob', request_id='hire-bob')
    assert supervisor.provision('alice', 1)['ready']
    assert supervisor.provision('bob', 1)['ready']
    assert len(backend.instances) == 2
    assert supervisor.deactivate(actor=actor, member_id='alice', request_id='retire-alice')['phase'] == 'parked'
    assert supervisor.status('bob', 1)['ready']
    assert backend.instances[('team', 'bob', 1)][0] == 'ready'


def test_inactive_with_newly_observed_external_work_is_not_parked(context, monkeypatch):
    registry, actor, host, backend, supervisor, directory = context
    supervisor.provision('alice', 1)
    registry.request_deactivation(actor=actor, member_id='alice', request_id='retire')
    registry.finalize_deactivation(supervisor=host, member_id='alice', generation=1, external_idle=True)
    backend.idle_value = False
    result = supervisor.reconcile_deactivation('alice', 1)
    assert result['phase'] == 'parking' and result['membership_status'] == 'inactive'
    assert backend.park_calls == 0
    backend.idle_value = True
    assert supervisor.reconcile_deactivation('alice', 1)['phase'] == 'parked'


def test_backend_fork_does_not_keep_parent_guard_alive(context):
    import os
    registry, actor, host, backend, supervisor, directory = context
    read_ready, write_ready = os.pipe()
    read_release, write_release = os.pipe()
    real_ensure = backend.ensure
    children = []
    def fork_during_ensure(spec):
        pid = os.fork()
        if pid == 0:
            try:
                os.close(read_ready)
                os.close(write_release)
                os.write(write_ready, b'R')
                os.read(read_release, 1)
            finally:
                os._exit(0)
        children.append(pid)
        assert os.read(read_ready, 1) == b'R'
        real_ensure(spec)
    backend.ensure = fork_during_ensure
    try:
        assert supervisor.provision('alice', 1)['ready']
        other = Provisioner(registry, supervisor=host, backend=backend, state_dir=directory, lock_timeout=0.2)
        assert other.status('alice', 1)['ready']
    finally:
        os.write(write_release, b'X')
        for fd in (read_ready, write_ready, read_release, write_release):
            os.close(fd)
        for pid in children:
            os.waitpid(pid, 0)


def test_busy_guard_has_finite_timeout_and_deactivation_still_freezes(context):
    registry, actor, host, backend, supervisor, directory = context
    other = Provisioner(registry, supervisor=host, backend=backend, state_dir=directory, lock_timeout=0.05)
    spec, _ = supervisor._spec('alice', 1)
    with supervisor._guard(spec):
        with pytest.raises(TimeoutError, match='member lifecycle guard busy'):
            other.deactivate(actor=actor, member_id='alice', request_id='retire')
        assert registry.role_for_member(supervisor=host, member_id='alice', generation=1)['status'] == 'draining'
    assert other.reconcile_deactivation('alice', 1)['phase'] == 'parked'


def test_phase_rejects_symlinks_permissive_files_and_fifo(context, tmp_path):
    import os
    registry, actor, host, backend, supervisor, directory = context
    spec, _ = supervisor._spec('alice', 1)
    journal = directory / (supervisor._name(spec) + '.json')
    outside = tmp_path / 'outside'
    outside.write_text('untouched')
    journal.symlink_to(outside)
    with pytest.raises(OSError):
        supervisor.status('alice', 1)
    assert outside.read_text() == 'untouched'
    journal.unlink()
    journal.write_text('{}')
    journal.chmod(0o644)
    with pytest.raises(ValueError, match='private_state_file_required'):
        supervisor.status('alice', 1)
    journal.unlink()
    os.mkfifo(journal, 0o600)
    with pytest.raises(ValueError, match='private_state_file_required'):
        supervisor.status('alice', 1)


def test_status_rechecks_membership_after_slow_observation(context):
    registry, actor, host, backend, supervisor, directory = context
    assert supervisor.provision('alice', 1)['ready']
    original = backend.inspect
    def inspect_and_freeze(spec):
        result = original(spec)
        registry.request_deactivation(actor=actor, member_id='alice', request_id='during-inspection')
        return result
    backend.inspect = inspect_and_freeze
    result = supervisor.status('alice', 1)
    assert result['membership_status'] == 'draining' and result['ready'] is False


@pytest.mark.parametrize('bad', [-1, True, None, float('nan'), float('inf')])
def test_invalid_lock_timeout_rejected(context, bad):
    registry, actor, host, backend, supervisor, directory = context
    with pytest.raises(ValueError, match='invalid_lock_timeout'):
        Provisioner(registry, supervisor=host, backend=backend, state_dir=directory, lock_timeout=bad)
