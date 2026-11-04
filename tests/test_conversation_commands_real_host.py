"""Real host command registration in a temporary profile, without transport/model calls."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]

def test_real_plugin_manager_registers_noncolliding_commands(tmp_path):
    host_var = os.environ.get('HERMES_SOURCE')
    if not host_var:
        pytest.skip('set HERMES_SOURCE to exercise actual PluginManager registration')
    host = Path(host_var).resolve()
    home = tmp_path / 'home'
    profile = home / 'profiles/canary'
    profile.mkdir(parents=True)
    settings = {'enabled': True, 'engine_root': str(ROOT),
                'liveness_db': str(tmp_path / 'liveness.db'),
                'store_path': str(tmp_path / 'cards.db'),
                'foreground_min_seconds': 99999, 'poll_seconds': 2}
    (profile / 'config.yaml').write_text(json.dumps(
        {'plugins': {'entries': {'task-progress': {'settings': settings}}}}))
    site_packages = host / 'venv/lib' / ('python%d.%d' % sys.version_info[:2]) / 'site-packages'
    env = dict(os.environ, HOME=str(tmp_path), HERMES_HOME=str(home),
               HERMES_ENABLE_PROJECT_PLUGINS='0', HERMES_DISABLE_LAZY_INSTALLS='1')
    env['PYTHONPATH'] = os.pathsep.join([str(host), str(ROOT), str(site_packages)])
    for key in ('HERMES_PROFILE', 'HERMES_SESSION_ID', 'HERMES_SESSION_KEY'):
        env.pop(key, None)
    script = r'''
import sys
from pathlib import Path
from hermes_cli.plugins import PluginContext, PluginManager
from hermes_cli.plugins_manifest import PluginManifest
from hermes_cli.commands import resolve_command
root, profile = Path(sys.argv[1]), Path(sys.argv[2])
manager = PluginManager(scope_key=str(profile))
manifest = PluginManifest(name='task-progress', key='task-progress', source='user',
                          path=str(root/'plugins/task_progress'))
manager._load_plugin(manifest)
record = manager._plugins['task-progress']
assert not record.error, record.error
commands = manager._plugin_commands
for primary, fallback in [('status','taskstatus'),('pause','taskpause'),('cancel','taskcancel')]:
    name = fallback if resolve_command(primary) is not None else primary
    assert name in commands, (name, list(commands))
    assert resolve_command(name) is None
    assert '/'+name+' ' in commands[name]['description']
    assert '/'+name+' ' in commands[name]['handler']('')
    if primary in ('pause','cancel'):
        result = commands[name]['handler']('proc_fake')
        assert '未接线' in result and '未改变' in result
        assert '已记录' not in result
assert not any(resolve_command(name) is not None for name in commands)
print('real-command-registration-passed')
'''
    result = subprocess.run([sys.executable, '-c', script, str(ROOT), str(profile)],
                            env=env, cwd=tmp_path, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'real-command-registration-passed' in result.stdout
