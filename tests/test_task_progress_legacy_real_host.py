"""Real installed-host routing, with only adapter network I/O replaced."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_legacy_host_posts_and_edits_existing_session_without_post_message(tmp_path):
    host = os.environ.get('HERMES_SOURCE')
    if not host:
        pytest.skip('set HERMES_SOURCE for installed-host route integration')
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text(json.dumps({'plugins': {'enabled': []}}))
    (home / '.env').write_text('FEISHU_ALLOWED_USERS=probe-user\n')
    env = dict(os.environ, HOME=str(tmp_path), HERMES_HOME=str(home),
               HERMES_DISABLE_LAZY_INSTALLS='1', HERMES_ENABLE_PROJECT_PLUGINS='0',
               PYTHONPATH=os.pathsep.join([host, str(ROOT)]))
    for key in ('HERMES_PROFILE', 'HERMES_SESSION_ID', 'HERMES_SESSION_KEY'):
        env.pop(key, None)
    script = r'''
import asyncio, importlib.util, sys, time
from pathlib import Path
from unittest.mock import AsyncMock
from types import SimpleNamespace
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli.plugins import PluginContext, PluginManager
from hermes_cli.plugins_manifest import PluginManifest
root, host, home = map(Path, sys.argv[1:])
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
plugin = load('_real_legacy_progress', root / 'plugins/task_progress/__init__.py')
feishu = load('_real_legacy_feishu', host / 'plugins/platforms/feishu/adapter.py')
runner = GatewayRunner(GatewayConfig())
runner._running = True
runner._draining = False
adapter = feishu.FeishuAdapter(PlatformConfig(enabled=True, extra={'card_replies': True}))
adapter._running = True
adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id='card-1'))
adapter.edit_message = AsyncMock(return_value=SimpleNamespace(success=True, message_id='card-1'))
adapter._card_message_ids = {'card-1': time.time()}
runner.adapters[Platform.FEISHU] = adapter
source = SessionSource(platform=Platform.FEISHU, chat_id='probe-chat', user_id='probe-user')
entry = runner.session_store.get_or_create_session(source)
manager = PluginManager(scope_key=str(home))
ctx = PluginContext(PluginManifest(name='task-progress', key='task-progress',
    source='user', path=root / 'plugins/task_progress'), manager)
# Exercise the fallback even if a future host introduces the preferred API.
ctx.post_message = None
async def check():
    runner._gateway_loop = asyncio.get_running_loop()
    posted = await asyncio.to_thread(plugin._post_to_session, ctx, entry.session_key,
                                      'Nonempty progress', allow_bound_context=True)
    assert posted.get('ok'), posted
    assert posted['message_id'] == 'card-1'
    assert adapter.send.await_count == 1
    assert adapter.send.call_args.kwargs['metadata']['expect_edits'] is True
    edited = await asyncio.to_thread(plugin._edit_to_session, ctx, entry.session_key,
                                     'card-1', 'Completed progress')
    assert edited.get('ok'), edited
    assert adapter.edit_message.await_count == 1
    assert adapter.send.await_count == 1, 'edit must not post a second message'
    ctx._manager = SimpleNamespace(home_path=home / 'unserved-profile')
    refused = await asyncio.to_thread(plugin._post_to_session, ctx, entry.session_key, 'wrong profile')
    assert not refused.get('ok'), refused
    assert adapter.send.await_count == 1
asyncio.run(check())
print('real-legacy-route-passed')
'''
    result = subprocess.run([os.environ.get('HERMES_PYTHON', sys.executable), '-c', script,
                             str(ROOT), host, str(home)], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'real-legacy-route-passed' in result.stdout
