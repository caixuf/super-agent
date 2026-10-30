"""Offline Hermes profile staging; intentionally NOT an executable worker backend.

This implementation makes a clean, channel-free profile in a bubblewrap sandbox.
It never injects credentials or launches an agent, gateway or tool. `inspect`
therefore never reports ready and `idle` cannot certify an external queue.
Host configuration and the installed Hermes source are trusted inputs.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess

from agentcore.colleague_provisioner import InstanceSpec
from agentcore.colleague_registry import Supervisor

_CREATE = r'''
import json, os, sys
os.umask(0o077)
sys.path[:0] = ['/opt/hermes', sys.argv[1]]
from hermes_cli.profiles import create_profile
payload = json.load(sys.stdin)
# create_profile() takes no soul= argument, so the role text goes in afterwards. The umask
# above is what keeps SOUL.md at 0600: a fresh install publishes it as 0664, and the
# verifier only accepts a private profile.
profile = create_profile('worker', no_alias=True, no_skills=True)
with open(os.path.join(profile, 'SOUL.md'), 'w', encoding='utf-8') as out:
    out.write(payload['soul'])
'''


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _private_dir(path):
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077
            or (hasattr(os, 'getuid') and info.st_uid != os.getuid())):
        raise ValueError('private_staging_directory_required')


def _read_private(path, maximum=65536):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'r', encoding='utf-8') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1
                or info.st_size > maximum
                or (hasattr(os, 'getuid') and info.st_uid != os.getuid())):
            raise ValueError('private_staging_file_required')
        return stream.read(maximum + 1)


class StagedHermesBackend:
    """Unready backend for verified-offline profile creation only.

    Root, Hermes source, Python, site-packages and bwrap are selected by the
    trusted host. No model-provided path, executable, credential or option is
    accepted. All calls for one identity must also use Provisioner's guard.
    """

    def __init__(self, *, registry, supervisor, root, hermes_source, python, site_packages, bwrap='/usr/bin/bwrap', timeout=30):
        if type(supervisor) is not Supervisor or supervisor.profile_id != registry.profile_id:
            raise PermissionError('trusted_supervisor_required')
        self.registry, self.supervisor = registry, supervisor
        self.root = Path(root)
        if not self.root.is_absolute():
            raise ValueError('absolute_staging_root_required')
        _private_dir(self.root)
        self.source = Path(hermes_source).resolve(strict=True)
        self.python = Path(python).resolve(strict=True)
        self.site_packages = Path(site_packages).resolve(strict=True)
        if not self.source.is_dir() or not self.site_packages.is_dir() or not self.python.is_file():
            raise ValueError('invalid_hermes_installation')
        try:
            self._python_inside = '/opt/hermes/' + self.python.relative_to(self.source).as_posix()
            self._site_inside = '/opt/hermes/' + self.site_packages.relative_to(self.source).as_posix()
        except ValueError:
            raise ValueError('hermes_paths_outside_installation') from None
        self.bwrap = Path(bwrap)
        if not self.bwrap.is_absolute() or not self.bwrap.is_file():
            raise ValueError('bubblewrap_required')
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not 0 < timeout <= 300:
            raise ValueError('invalid_staging_timeout')
        self.timeout = timeout

    def _identity(self, spec):
        if type(spec) is not InstanceSpec:
            raise TypeError('host_instance_spec_required')
        identity = [spec.profile_id, spec.member_id, spec.generation]
        if (not isinstance(spec.profile_id, str) or not isinstance(spec.member_id, str)
                or type(spec.generation) is not int or spec.generation < 1
                or spec.role_id == 'primary'):
            raise ValueError('invalid_instance_identity')
        if spec.profile_id != self.registry.profile_id:
            raise PermissionError('wrong_instance_profile')
        role = self.registry.role_for_member(supervisor=self.supervisor, member_id=spec.member_id, generation=spec.generation)
        if spec.member_id == self.registry.primary_member(supervisor=self.supervisor) or role['role_id'] == 'primary':
            raise PermissionError('primary_protected')
        if (spec.role_id, spec.display_name, spec.duty, spec.boundaries) != (
                role['role_id'], role['display_name'], role['duty'], tuple(role['boundaries'])):
            raise ValueError('instance_spec_mismatch')
        key = hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()
        soul = ('# Role: ' + spec.display_name + '\n\nDuty: ' + spec.duty + '\n\nBoundaries:\n'
                + ''.join('- ' + item + '\n' for item in spec.boundaries))
        digest = hashlib.sha256(json.dumps([identity, spec.role_id, soul], ensure_ascii=False,
                                          separators=(',', ':')).encode()).hexdigest()
        return key, identity, digest, soul

    def _paths(self, spec):
        key, identity, digest, soul = self._identity(spec)
        instance = self.root / key
        return instance, identity, digest, soul

    def _verify_marker(self, instance, identity, digest):
        _private_dir(self.root)
        _private_dir(instance)
        marker = instance / 'identity.json'
        data = json.loads(_read_private(marker, maximum=4096))
        if data != {'identity': identity, 'digest': digest}:
            raise ValueError('instance_identity_conflict')

    def _verify_profile(self, instance, soul):
        home = instance / 'home'
        root = home / '.hermes'
        profiles = root / 'profiles'
        profile = profiles / 'worker'
        for path in (home, root, profiles, profile, profile / 'memories'):
            _private_dir(path)
        env_text = _read_private(profile / '.env')
        soul_text = _read_private(profile / 'SOUL.md')
        _read_private(profile / '.no-bundled-skills')
        if soul_text != soul:
            raise ValueError('instance_role_conflict')
        if any(line.strip() and not line.strip().startswith('#') for line in env_text.splitlines()):
            raise ValueError('unexpected_profile_credentials')
        if any((profile / name).exists() or (profile / name).is_symlink() for name in ('config.yaml', 'plugins')):
            raise ValueError('unexpected_profile_configuration')
        if any((profile / 'memories').iterdir()):
            raise ValueError('unexpected_initial_memory')

    def ensure(self, spec):
        instance, identity, digest, soul = self._paths(spec)
        role = self.registry.role_for_member(supervisor=self.supervisor, member_id=spec.member_id, generation=spec.generation)
        if role['status'] != 'active':
            raise ValueError('member_not_active')
        _private_dir(self.root)
        try:
            instance.mkdir(mode=0o700)
            _sync_dir(self.root)
        except FileExistsError:
            self._verify_marker(instance, identity, digest)
        else:
            marker = instance / 'identity.json'
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                json.dump({'identity': identity, 'digest': digest}, out, sort_keys=True)
                out.flush()
                os.fsync(out.fileno())
            _sync_dir(instance)
        self._verify_marker(instance, identity, digest)
        home = instance / 'home'
        for directory in (home, home / '.hermes'):
            try:
                directory.mkdir(mode=0o700)
                _sync_dir(directory.parent)
            except FileExistsError:
                pass
            _private_dir(directory)
        profile = home / '.hermes' / 'profiles' / 'worker'
        if profile.exists() or profile.is_symlink():
            self._verify_profile(instance, soul)
            return
        # No host env, home, network, socket or credentials are made available.
        argv = [str(self.bwrap), '--unshare-all', '--die-with-parent', '--new-session',
                '--clearenv', '--ro-bind', '/usr', '/usr', '--ro-bind', '/lib', '/lib',
                '--ro-bind', '/lib64', '/lib64', '--proc', '/proc', '--dev', '/dev',
                '--tmpfs', '/tmp', '--ro-bind', str(self.source), '/opt/hermes',
                '--bind', str(home), '/sandbox', '--chdir', '/sandbox',
                '--setenv', 'HOME', '/sandbox', '--setenv', 'HERMES_HOME', '/sandbox/.hermes',
                '--setenv', 'PATH', '/usr/bin:/bin', '--setenv', 'TMPDIR', '/tmp', '--',
                self._python_inside, '-I', '-B', '-c', _CREATE, self._site_inside]
        result = subprocess.run(argv, input=json.dumps({'soul': soul}, ensure_ascii=False), text=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env={'PATH': '/usr/bin:/bin'}, timeout=self.timeout, check=False)
        if result.returncode:
            raise RuntimeError('hermes_offline_staging_failed')
        self._verify_profile(instance, soul)

    def inspect(self, spec):
        instance, identity, digest, soul = self._paths(spec)
        if not instance.exists() and not instance.is_symlink():
            return {'state': 'absent'}
        self._verify_marker(instance, identity, digest)
        profile = instance / 'home' / '.hermes' / 'profiles' / 'worker'
        if profile.exists() or profile.is_symlink():
            self._verify_profile(instance, soul)
        return {'state': 'starting'}  # Offline profile is never an operational worker.

    def idle(self, spec):
        self._paths(spec)
        return False  # No authoritative view of external queues or pending delivery.

    def park(self, spec):
        self._paths(spec)
        raise RuntimeError('hermes_runtime_not_connected')
