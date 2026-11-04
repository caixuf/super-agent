from pathlib import Path
import os

import pytest

from agentcore.colleague_hermes_staging import StagedHermesBackend
from agentcore.colleague_provisioner import InstanceSpec, Provisioner
from agentcore.colleague_registry import Registry, Actor, Supervisor


SOURCE = Path(os.environ.get('HERMES_STAGING_SOURCE', '~/.hermes/hermes-agent')).expanduser()
PYTHON = SOURCE / 'venv/bin/python'
SITES = list((SOURCE / 'venv/lib').glob('python*/site-packages'))


@pytest.fixture
def backend(tmp_path):
    # Host-coupled by design: the sandbox needs a real Hermes install to copy in, and bubblewrap
    # to isolate it. Point HERMES_STAGING_SOURCE at an install (any path, not a personal one) to
    # exercise this; the default is the standard install location. A skip here means "no runtime
    # to test against", never "the sandbox works".
    if not (SOURCE.is_dir() and PYTHON.exists() and SITES and Path('/usr/bin/bwrap').is_file()):
        pytest.skip('no Hermes install with a venv, or no bubblewrap: set HERMES_STAGING_SOURCE '
                    '(looked at %s)' % SOURCE)
    registry = Registry(tmp_path / 'registry.sqlite', profile_id='team', primary_id='leader')
    actor, host = Actor('team', 'leader'), Supervisor('team')
    registry.define_role(actor=actor, role_id='review', label='Review', duty='Check reports',
                         boundaries=['Read only'], request_id='role')
    registry.hire(actor=actor, member_id='alice', role_id='review', display_name='Alice', request_id='hire')
    registry.hire(actor=actor, member_id='bob', role_id='review', display_name='Bob', request_id='hire-bob')
    return StagedHermesBackend(registry=registry, supervisor=host, root=tmp_path,
                               hermes_source=SOURCE, python=PYTHON, site_packages=SITES[0])


@pytest.fixture
def spec():
    return InstanceSpec('team', 'alice', 1, 'review', 'Alice', 'Check reports', ('Read only',))


def test_offline_profile_is_real_but_never_ready_and_has_no_credentials(backend, spec, monkeypatch):
    monkeypatch.setenv('FAKE_PERSONAL_TOKEN', 'credential_must_not_be_copied')
    assert backend.inspect(spec)['state'] == 'absent'
    backend.ensure(spec)
    assert backend.inspect(spec)['state'] == 'starting'
    assert backend.idle(spec) is False
    instance, _, _, _ = backend._paths(spec)
    profile = instance / 'home/.hermes/profiles/worker'
    assert (profile / 'memories').is_dir() and (profile / 'SOUL.md').is_file()
    assert 'Check reports' in (profile / 'SOUL.md').read_text()
    assert not (profile / 'config.yaml').exists()
    assert not (profile / 'plugins').exists()
    assert 'credential_must_not_be_copied' not in (profile / '.env').read_text()
    snapshot = (profile / 'SOUL.md').stat().st_ino
    backend.ensure(spec)
    assert (profile / 'SOUL.md').stat().st_ino == snapshot
    with pytest.raises(RuntimeError, match='hermes_runtime_not_connected'):
        backend.park(spec)


def test_provisioner_never_reports_ready_or_parks_offline_instance(backend, tmp_path):
    registry = backend.registry
    actor, host = Actor('team', 'leader'), Supervisor('team')
    state = tmp_path / 'state'; state.mkdir(mode=0o700)
    supervisor = Provisioner(registry, supervisor=host, backend=backend, state_dir=state)
    status = supervisor.provision('alice', 1)
    assert status['phase'] == 'starting' and not status['ready']
    status = supervisor.deactivate(actor=actor, member_id='alice', request_id='retire')
    assert status['membership_status'] == 'draining' and status['phase'] == 'draining'


def test_instances_are_separate_and_role_conflicts_fail_closed(backend, spec):
    backend.ensure(spec)
    other = InstanceSpec('team', 'bob', 1, 'review', 'Bob', 'Check reports', ('Read only',))
    backend.ensure(other)
    assert backend._paths(spec)[0] != backend._paths(other)[0]
    changed = InstanceSpec('team', 'alice', 1, 'review', 'Alice', 'Changed', ('Read only',))
    with pytest.raises(ValueError, match='instance_spec_mismatch'):
        backend.ensure(changed)


def test_corruption_permissions_and_symlinks_fail_closed(backend, spec, tmp_path):
    instance, _, _, _ = backend._paths(spec)
    instance.symlink_to(tmp_path / 'outside')
    with pytest.raises(ValueError, match='private_staging_directory_required'):
        backend.ensure(spec)
    instance.unlink()
    backend.ensure(spec)
    profile = instance / 'home/.hermes/profiles/worker'
    env = profile / '.env'
    env.write_text('EXTERNAL_TOKEN=secret\n')
    with pytest.raises(ValueError, match='unexpected_profile_credentials'):
        backend.inspect(spec)
    env.write_text('# blank\n')
    env.chmod(0o644)
    with pytest.raises(ValueError, match='private_staging_file_required'):
        backend.ensure(spec)
    env.unlink()
    outside = tmp_path / 'outside'; outside.write_text('untouched')
    env.symlink_to(outside)
    with pytest.raises(OSError):
        backend.inspect(spec)
    assert outside.read_text() == 'untouched'


def test_root_privacy_and_primary_role_are_checked(backend, spec):
    with pytest.raises(ValueError, match='invalid_instance_identity'):
        backend.ensure(InstanceSpec('team', 'leader', 1, 'primary', 'Leader', 'Protect', ('Read only',)))
    with pytest.raises(PermissionError, match='primary_protected'):
        backend.ensure(InstanceSpec('team', 'leader', 1, 'review', 'Leader', 'Check reports', ('Read only',)))
    backend.root.chmod(0o755)
    with pytest.raises(ValueError, match='private_staging_directory_required'):
        backend.ensure(spec)


def test_failed_creation_retries_without_replacing_profile(backend, spec, monkeypatch):
    import agentcore.colleague_hermes_staging as module
    original = module.subprocess.run
    def create_then_lose_response(*args, **kwargs):
        result = original(*args, **kwargs)
        assert result.returncode == 0
        raise TimeoutError('lost response')
    monkeypatch.setattr(module.subprocess, 'run', create_then_lose_response)
    with pytest.raises(TimeoutError):
        backend.ensure(spec)
    monkeypatch.setattr(module.subprocess, 'run', original)
    backend.ensure(spec)
    assert backend.inspect(spec)['state'] == 'starting'


def test_real_sandbox_cannot_see_host_file_or_reach_network(backend, spec, tmp_path, monkeypatch):
    import json
    import agentcore.colleague_hermes_staging as module
    secret = tmp_path / 'host_private_marker'
    secret.write_text('private-marker')
    module_code = module._CREATE + """
import json, socket
visible = os.path.exists(""" + repr(str(secret)) + """)
sock = socket.socket()
sock.settimeout(0.4)
try:
    sock.connect(('203.0.113.1', 443))
    reached = True
except OSError:
    reached = False
finally:
    sock.close()
with open('/sandbox/offline-probe.json', 'w') as out:
    json.dump({'host_visible': visible, 'network_reached': reached}, out)
"""
    monkeypatch.setattr(module, '_CREATE', module_code)
    backend.ensure(spec)
    instance, _, _, _ = backend._paths(spec)
    proof = json.loads((instance / 'home/offline-probe.json').read_text())
    assert proof == {'host_visible': False, 'network_reached': False}


def test_recover_after_marker_before_home_is_created(backend, spec, monkeypatch):
    instance, identity, digest, _ = backend._paths(spec)
    instance.mkdir(mode=0o700)
    marker = instance / 'identity.json'
    import json
    marker.write_text(json.dumps({'identity': identity, 'digest': digest}))
    marker.chmod(0o600)
    backend.ensure(spec)
    assert backend.inspect(spec)['state'] == 'starting'


def test_marker_corruption_never_replaces_existing_data(backend, spec):
    instance, _, _, _ = backend._paths(spec)
    instance.mkdir(mode=0o700)
    marker = instance / 'identity.json'
    marker.write_text('{')
    marker.chmod(0o600)
    with pytest.raises(ValueError):
        backend.ensure(spec)
    assert marker.read_text() == '{'


def test_reject_python_or_site_packages_outside_trusted_installation(backend, tmp_path):
    with pytest.raises(ValueError, match='hermes_paths_outside_installation'):
        StagedHermesBackend(registry=backend.registry, supervisor=backend.supervisor, root=tmp_path, hermes_source=SOURCE, python='/usr/bin/python3', site_packages=SITES[0])
