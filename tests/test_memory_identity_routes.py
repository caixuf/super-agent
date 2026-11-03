"""Authenticated IM memory routing; no live profiles, models or transports."""
from __future__ import annotations

from contextlib import contextmanager
import contextvars
import importlib.util
import json
import io
import os
from pathlib import Path
import sys
import types
import threading
import subprocess
import tarfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from agentcore.memory.config import MemoryConfig, configure, reset_config
from agentcore.memory.memory_store import write_fact

REPO = Path(__file__).resolve().parents[1]
HOST = Path.home() / ".hermes/hermes-agent"


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def host_context(monkeypatch, tmp_path):
    """Small offline ContextVar contract; actual host modules are tested below."""
    paths = ("gateway/session_context.py", "hermes_constants.py", "agent/delegation_context.py")
    session = types.ModuleType('gateway.session_context')
    keys = ('PLATFORM', 'SCOPE_ID', 'USER_ID', 'CHAT_ID', 'CHAT_TYPE', 'KEY', 'ID')
    session._VAR_MAP = {'HERMES_SESSION_' + k: contextvars.ContextVar(k, default=None) for k in keys}
    session._VAR_MAP['HERMES_CRON_SESSION'] = contextvars.ContextVar('cron', default=None)
    def set_session_vars(**values):
        values = {'KEY' if k == 'session_key' else 'ID' if k == 'session_id' else k.upper(): v
                  for k, v in values.items()}
        return [var.set(values.get(name.removeprefix('HERMES_SESSION_') if name != 'HERMES_CRON_SESSION'
                                    else 'CRON_SESSION', '')) for name, var in session._VAR_MAP.items()]
    session.set_session_vars = set_session_vars
    session.reset_session_vars = lambda: [var.set(None) for var in session._VAR_MAP.values()]
    constants = types.ModuleType('hermes_constants')
    override = contextvars.ContextVar('identity_test_home', default=None)
    constants.set_hermes_home_override = override.set
    constants.reset_hermes_home_override = override.reset
    constants.get_hermes_home_override = override.get
    constants.get_hermes_home = lambda: Path(override.get())
    delegation = types.ModuleType('agent.delegation_context')
    delegation.is_delegated_child_process_context = lambda: False
    multiplex = contextvars.ContextVar("memory_test_multiplex", default=False)
    secret_scope = types.ModuleType("agent.secret_scope")
    secret_scope.is_multiplex_active = multiplex.get
    gateway = types.ModuleType("gateway")
    gateway.session_context = session
    for name, module in (("gateway", gateway), ("gateway.session_context", session),
                         ("hermes_constants", constants),
                         ("agent.delegation_context", delegation),
                         ("agent.secret_scope", secret_scope)):
        monkeypatch.setitem(sys.modules, name, module)
    home = tmp_path / "profile"
    home.mkdir(mode=0o700)

    @contextmanager
    def bind(user="alice-im", chat="alice-chat", *, sid="s1", chat_type="dm", **values):
        token = constants.set_hermes_home_override(home)
        tokens = session.set_session_vars(
            platform="test-im", scope_id="workspace", user_id=user, chat_id=chat,
            chat_type=chat_type, session_id=sid, session_key="route:" + sid,
            cron_session="", **values)
        try:
            yield
        finally:
            for item in reversed(tokens):
                item.var.reset(item)
            constants.reset_hermes_home_override(token)

    return types.SimpleNamespace(session=session, constants=constants, delegation=delegation,
                                 multiplex=multiplex, home=home, bind=bind)


class Context:
    profile_name = "p1"

    def __init__(self, settings):
        self.settings, self.tools, self.hooks = settings, {}, {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs

    def register_hook(self, name, callback):
        self.hooks[name] = callback


def prepare(tmp_path, host):
    bindings, ids = [], {}
    for person in ("alice", "bob"):
        state = tmp_path / person
        state.mkdir(mode=0o700)
        (state / "spool").mkdir(mode=0o700)
        data = state / "data"
        data.mkdir(mode=0o700)
        configure(MemoryConfig(data_dir=data, default_user=person, default_agent="assistant",
                               embed_fn=lambda texts: None))
        try:
            ids[person] = write_fact(person + " private secret", subject="private",
                                    user_id=person, agent_id="assistant",
                                    tags={"profile": "p1"}, schedule_background_index=False)["fact_id"]
        finally:
            reset_config()
        bindings.append({"platform": "test-im", "scope_id": "workspace",
                         "user_id": person + "-im", "chat_id": person + "-chat",
                         "principal": person, "agent_id": "assistant", "state_dir": str(state)})
    policy = host.home / "memory-identities.json"
    policy.write_text(json.dumps({"version": 1, "profile": "p1",
                                  "profile_home": str(host.home), "bindings": bindings}))
    policy.chmod(0o600)
    # Legacy fields deliberately remain: routed mode must not fall back to them.
    ctx = Context({"enabled": True, "engine_root": str(REPO),
                   "identity_routes_file": str(policy),
                   "spool_dir": str(tmp_path / "legacy-spool"),
                   "data_dir": str(tmp_path / "alice/data"),
                   "user_id": "alice", "agent_id": "assistant"})
    return ctx, policy, ids


def test_unknown_user_cannot_read_legacy_owner_store(tmp_path, host_context):
    ctx, policy, ids = prepare(tmp_path, host_context)
    plugin = load_file("identity_bridge_test", REPO / "plugins/memory_bridge/__init__.py")
    plugin.register(ctx)
    handler = ctx.tools["memory_get_observations_l3"]["handler"]
    with host_context.bind(user="mallory", chat="mallory-chat"):
        result = json.loads(handler({"ids": [ids["alice"]]}, session_id="s1"))
    assert result == {"ok": False, "error": "memory_identity_required"}
    assert not (tmp_path / "legacy-spool").exists()


def test_two_authenticated_users_read_only_own_store_and_capture_separate_spools(tmp_path, host_context):
    ctx, policy, ids = prepare(tmp_path, host_context)
    plugin = load_file("identity_bridge_test", REPO / "plugins/memory_bridge/__init__.py")
    plugin.register(ctx)
    handler = ctx.tools["memory_get_observations_l3"]["handler"]
    def run(person):
        with host_context.bind(user=person + '-im', chat=person + '-chat'):
            result = json.loads(handler({'ids': list(ids.values())}, session_id='s1'))
            ctx.hooks['post_llm_call'](session_id='s1', turn_id='same-turn',
                                       user_message=person + ' input', assistant_response='ack')
            ctx.hooks['on_session_end'](session_id='s1', turn_id='same-turn', completed=True)
            return [r['fact_id'] for r in result['results']]
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(run, ('alice', 'bob')))
    assert results == [[ids['alice']], [ids['bob']]]
    for person in ('alice', 'bob'):
        rows = [json.loads(p.read_text()) for p in (tmp_path / person / 'spool').glob('*.json')]
        assert len(rows) == 1 and rows[0]['state'] == 'completed'
        assert rows[0]['user_id'] == person and rows[0]['user_message'] == person + ' input'
    assert not (tmp_path / 'legacy-spool').exists()


@pytest.mark.parametrize('case', ['unknown', 'group', 'session', 'profile', 'env-only', 'multiplex', 'delegated'])
def test_missing_or_wrong_authenticated_context_never_runs_tool_or_capture(tmp_path, host_context, monkeypatch, case):
    ctx, policy, ids = prepare(tmp_path, host_context)
    plugin = load_file('identity_bridge_test', REPO / 'plugins/memory_bridge/__init__.py')
    plugin.register(ctx)
    import subprocess
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('unauthorized subprocess'))
    values = {'user': 'unknown'} if case == 'unknown' else {}
    if case == 'group':
        values['chat_type'] = 'group'
    with host_context.bind(**values):
        if case == 'profile':
            monkeypatch.setattr(host_context.constants, 'get_hermes_home', lambda: tmp_path)
        elif case == 'env-only':
            host_context.session.reset_session_vars()
            for key, value in {'USER_ID': 'alice-im', 'CHAT_ID': 'alice-chat',
                               'PLATFORM': 'test-im', 'SCOPE_ID': 'workspace', 'ID': 's1',
                               'KEY': 'route:s1', 'CHAT_TYPE': 'dm'}.items():
                monkeypatch.setenv('HERMES_SESSION_' + key, value)
        elif case == 'multiplex':
            monkeypatch.setattr(sys.modules['agent.secret_scope'], 'is_multiplex_active', lambda: True)
            monkeypatch.setattr(host_context.constants, 'get_hermes_home_override', lambda: None)
        elif case == 'delegated':
            monkeypatch.setattr(host_context.delegation, 'is_delegated_child_process_context', lambda: True)
        sid = 'wrong' if case == 'session' else 's1'
        result = json.loads(ctx.tools['memory_get_observations_l3']['handler']({'ids': [ids['alice']]}, session_id=sid))
        assert result == {'ok': False, 'error': 'memory_identity_required'}
        ctx.hooks['post_llm_call'](session_id=sid, turn_id='t', user_message='private', assistant_response='ack')
        ctx.hooks['on_session_end'](session_id=sid, turn_id='t', completed=True)
    assert not list((tmp_path / 'alice/spool').glob('*.json'))


@pytest.mark.parametrize('case', ['missing', 'malformed', 'duplicate', 'ambiguous', 'shared', 'public', 'symlink'])
def test_broken_configured_policy_never_falls_back_to_legacy(tmp_path, host_context, monkeypatch, case):
    ctx, policy, ids = prepare(tmp_path, host_context)
    original = json.loads(policy.read_text())
    if case == 'missing':
        policy.unlink()
    elif case == 'malformed':
        policy.write_text('{')
    elif case == 'duplicate':
        policy.write_text('{"version":1,"version":1}')
    elif case == 'ambiguous':
        original['bindings'].append(original['bindings'][0])
        policy.write_text(json.dumps(original))
    elif case == 'shared':
        original['bindings'][1]['state_dir'] = original['bindings'][0]['state_dir']
        policy.write_text(json.dumps(original))
    elif case == 'public':
        policy.chmod(0o644)
    elif case == 'symlink':
        alias = policy.with_suffix('.alias')
        alias.symlink_to(policy)
        ctx.settings['identity_routes_file'] = str(alias)
    plugin = load_file('identity_bridge_test', REPO / 'plugins/memory_bridge/__init__.py')
    plugin.register(ctx)
    with host_context.bind():
        result = json.loads(ctx.tools['memory_get_observations_l3']['handler']({'ids': [ids['alice']]}, session_id='s1'))
        assert result == {'ok': False, 'error': 'memory_identity_required'}
    assert not (tmp_path / 'legacy-spool').exists()


def test_policy_revocation_before_verdict_does_not_complete_old_capture(tmp_path, host_context):
    ctx, policy, ids = prepare(tmp_path, host_context)
    plugin = load_file('identity_bridge_test', REPO / 'plugins/memory_bridge/__init__.py')
    plugin.register(ctx)
    with host_context.bind():
        ctx.hooks['post_llm_call'](session_id='s1', turn_id='t', user_message='private', assistant_response='ack')
        content = json.loads(policy.read_text())
        content['bindings'] = content['bindings'][1:]
        policy.write_text(json.dumps(content))
        ctx.hooks['on_session_end'](session_id='s1', turn_id='t', completed=True)
    record = json.loads(next((tmp_path / 'alice/spool').glob('*.json')).read_text())
    assert record['state'] == 'awaiting_verdict'
    assert not list((tmp_path / 'bob/spool').glob('*.json'))


def load_provider(monkeypatch):
    provider = types.ModuleType('agent.memory_provider')
    provider.MemoryProvider = object
    provider.RecallStatus = lambda **kw: types.SimpleNamespace(**kw)
    provider.is_trivial_prompt = lambda query: False
    def spawn(target, name, args):
        copied = contextvars.copy_context()
        return threading.Thread(target=lambda: copied.run(target, *args), name=name, daemon=True)
    provider.spawn_context_thread = spawn
    monkeypatch.setitem(sys.modules, 'agent.memory_provider', provider)
    return load_file('identity_provider_test', REPO / 'plugins/localvec/__init__.py')


def make_provider(tmp_path, host_context, monkeypatch):
    ctx, policy, ids = prepare(tmp_path, host_context)
    module = load_provider(monkeypatch)
    cfg = {'identity_routes_file': str(policy), 'engine_root': str(REPO), 'timeout_s': 2,
           'recall_cmd': ['must-never-execute-legacy'], 'sqlite_db': str(tmp_path / 'alice/data/coding_memory_cv.db'),
           'user_id': 'alice', 'agent_id': 'assistant', 'profile': 'p1'}
    monkeypatch.setattr(module, '_config_block', lambda: cfg)
    provider = module.LocalVecProvider()
    assert provider.is_available()
    provider.initialize('s1')
    return module, provider, policy


def test_automatic_recall_fallback_and_cache_are_principal_scoped(tmp_path, host_context, monkeypatch):
    module, provider, policy = make_provider(tmp_path, host_context, monkeypatch)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[argv.index('--user') + 1] in ('alice', 'bob')
        return types.SimpleNamespace(returncode=1, stdout='')
    monkeypatch.setattr(module.subprocess, 'run', run)
    def recall(person):
        with host_context.bind(user=person + '-im', chat=person + '-chat'):
            provider.queue_prefetch('private secret', session_id='s1')
            return provider.prefetch('private secret', session_id='s1')
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(recall, ('alice', 'bob')))
    assert 'alice private secret' in results[0] and 'bob private secret' not in results[0]
    assert 'bob private secret' in results[1] and 'alice private secret' not in results[1]
    assert len(calls) == 2
    with host_context.bind(user='unknown', chat='unknown'):
        assert provider.prefetch('private secret', session_id='s1') == module.RECALL_UNAVAILABLE
    assert len(calls) == 2


def test_routed_combined_recall_requires_authorized_route_and_disables_partial_fallback(tmp_path, host_context, monkeypatch):
    module, provider, policy = make_provider(tmp_path, host_context, monkeypatch)
    entity = tmp_path / "entities.db"
    entity.touch()
    provider._cfg["combined_entity_db"] = str(entity)
    with host_context.bind():
        child = provider._routed_provider("s1")
        assert child is not None
        assert child._cmd[1].endswith("tools/recall_combined.py")
        assert child._cmd[child._cmd.index("--entity-db") + 1] == str(entity)
        assert "sqlite_db" not in child._cfg
    with host_context.bind(user="other", chat="other"):
        assert provider._routed_provider("s1") is None


def test_policy_revocation_discards_inflight_automatic_result(tmp_path, host_context, monkeypatch):
    module, provider, policy = make_provider(tmp_path, host_context, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    def run(argv, **kwargs):
        entered.set()
        assert release.wait(3)
        return types.SimpleNamespace(returncode=0, stdout='{"results":[{"statement":"private secret"}]}')
    monkeypatch.setattr(module.subprocess, 'run', run)
    with host_context.bind():
        provider.queue_prefetch('private secret')
        child = next(iter(provider._routed_providers.values()))
        event = next(iter(child._inflight.values()))
        assert entered.wait(1)
        content = json.loads(policy.read_text())
        content['bindings'] = content['bindings'][1:]
        policy.write_text(json.dumps(content))
        release.set()
        assert event.wait(3)
        assert not child._cache and not event.recall_ok
        assert provider.prefetch('private secret') == module.RECALL_UNAVAILABLE


def test_automatic_cached_result_is_not_reused_after_policy_revocation(tmp_path, host_context, monkeypatch):
    module, provider, policy = make_provider(tmp_path, host_context, monkeypatch)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return types.SimpleNamespace(returncode=0, stdout='{"results":[{"statement":"private secret"}]}')
    monkeypatch.setattr(module.subprocess, 'run', run)
    with host_context.bind():
        provider.queue_prefetch('private secret')
        child = next(iter(provider._routed_providers.values()))
        event = next(iter(child._inflight.values()))
        assert event.wait(3) and child._cache
        policy.unlink()
        assert provider.prefetch('private secret') == module.RECALL_UNAVAILABLE
        assert len(calls) == 1


def test_unknown_user_automatic_recall_never_uses_legacy_even_with_broken_router(tmp_path, host_context, monkeypatch):
    module, provider, policy = make_provider(tmp_path, host_context, monkeypatch)
    policy.write_text('{')
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: pytest.fail('unauthorized recall'))
    with host_context.bind():
        provider.queue_prefetch('private secret')
        assert provider.prefetch('private secret') == module.RECALL_UNAVAILABLE
    assert not provider._routed_providers


def test_setup_prepares_two_private_workers_with_one_shared_authorization_policy(tmp_path, host_context):
    import yaml
    ctx, policy, ids = prepare(tmp_path, host_context)
    config = host_context.home / 'config.yaml'
    config.write_text('plugins:\n  enabled: [memory-bridge]\n')
    units = tmp_path / 'units'
    def setup(person, *extra):
        return subprocess.run([sys.executable, str(REPO / 'install/setup_memory.py'),
                               '--profile-home', str(host_context.home), '--engine-root', str(REPO),
                               '--profile', 'p1', '--user', person, '--agent', 'assistant',
                               '--embed-url', 'http://127.0.0.1:8910', '--unit-dir', str(units),
                               '--identity-routes-file', str(policy), *extra],
                              text=True, capture_output=True, timeout=20)
    assert setup('alice').returncode == 0
    assert not units.exists()
    for person in ('alice', 'bob', 'alice'):
        result = setup(person, '--apply')
        assert result.returncode == 0, result.stdout + result.stderr
    cfg = yaml.safe_load(config.read_text())
    settings = cfg['plugins']['entries']['memory-bridge']['settings']
    localvec = cfg['memory']['localvec']
    assert settings['identity_routes_file'] == localvec['identity_routes_file'] == str(policy)
    assert not set(settings) & {'user_id', 'agent_id', 'spool_dir', 'data_dir'}
    assert not set(localvec) & {'user_id', 'agent_id', 'sqlite_db', 'recall_cmd'}
    services = list(units.glob('*.service'))
    assert len(services) == 2
    contents = [unit.read_text() for unit in services]
    assert sum(str(tmp_path / 'alice/spool') in unit for unit in contents) == 1
    assert sum(str(tmp_path / 'bob/spool') in unit for unit in contents) == 1
    before = config.read_text()
    assert setup('unknown', '--apply').returncode != 0
    assert config.read_text() == before and len(list(units.glob('*.service'))) == 2


def test_routed_setup_refuses_implicit_single_owner_migration(tmp_path, host_context):
    ctx, policy, ids = prepare(tmp_path, host_context)
    config = host_context.home / 'config.yaml'
    original = 'plugins:\n  enabled: [memory-bridge]\nmemory:\n  localvec:\n    user_id: old-owner\n'
    config.write_text(original)
    units = tmp_path / 'units'
    result = subprocess.run([sys.executable, str(REPO / 'install/setup_memory.py'),
                             '--profile-home', str(host_context.home), '--engine-root', str(REPO),
                             '--profile', 'p1', '--user', 'alice', '--agent', 'assistant',
                             '--embed-url', 'http://127.0.0.1:8910', '--unit-dir', str(units),
                             '--identity-routes-file', str(policy), '--apply'],
                            text=True, capture_output=True, timeout=20)
    assert result.returncode != 0
    assert config.read_text() == original and not units.exists()


@pytest.mark.parametrize('value', [None, '', False, {}, 1])
def test_invalid_router_setting_is_not_legacy_opt_out(tmp_path, host_context, value):
    ctx, policy, ids = prepare(tmp_path, host_context)
    ctx.settings['identity_routes_file'] = value
    plugin = load_file('identity_bridge_test', REPO / 'plugins/memory_bridge/__init__.py')
    plugin.register(ctx)
    with host_context.bind():
        result = json.loads(ctx.tools['memory_get_observations_l3']['handler']({'ids': [ids['alice']]}, session_id='s1'))
        assert result == {'ok': False, 'error': 'memory_identity_required'}
    assert not (tmp_path / 'legacy-spool').exists()


def test_router_instance_never_downgrades_to_legacy_after_reinitialize(tmp_path, host_context, monkeypatch):
    module, provider, policy = make_provider(tmp_path, host_context, monkeypatch)
    monkeypatch.setattr(module, '_config_block', lambda: {'recall_cmd': ['legacy-owner-command']})
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: pytest.fail('unexpected legacy recall'))
    provider.initialize('s1')
    with host_context.bind():
        assert provider.prefetch('private secret') == module.RECALL_UNAVAILABLE
        assert provider.recall_status() is None


@pytest.mark.parametrize('case', ['exception', 'failed-defaults', 'failed-last-good', 'invalid-section'])
def test_unreadable_profile_config_never_adopts_legacy_environment_command(monkeypatch, case):
    module = load_provider(monkeypatch)
    config_module = types.ModuleType('hermes_cli.config')
    class FailedRead(dict):
        read_error = ValueError('private broken configuration')
    if case == 'exception':
        def read():
            raise ValueError('private broken configuration')
    elif case == 'invalid-section':
        read = lambda: {'memory': {'localvec': 'invalid'}}
    else:
        read = lambda: FailedRead({'memory': {'localvec': {'recall_cmd': ['old-owner']}}}
                                  if case == 'failed-last-good' else {})
    config_module.load_config_readonly = read
    monkeypatch.setitem(sys.modules, 'hermes_cli.config', config_module)
    monkeypatch.setenv('LOCALVEC_RECALL_CMD', 'environment-owner')
    provider = module.LocalVecProvider()
    assert not provider.is_available()
    provider.initialize('s1')
    assert not provider._cmd
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: pytest.fail('legacy fallback executed'))
    assert provider.prefetch('private query') == module.RECALL_UNAVAILABLE


def test_all_principals_share_a_bounded_subprocess_budget(tmp_path, host_context, monkeypatch):
    module, provider, policy = make_provider(tmp_path, host_context, monkeypatch)
    entered = threading.Condition()
    release = threading.Event()
    calls = []
    def run(argv, **kwargs):
        with entered:
            calls.append(argv)
            entered.notify_all()
        assert release.wait(4)
        return types.SimpleNamespace(returncode=0, stdout='{"results":[]}')
    monkeypatch.setattr(module.subprocess, 'run', run)
    events = []
    try:
        with host_context.bind():
            for n in range(module.MAX_INFLIGHT_RECALLS):
                provider.queue_prefetch('alice query ' + str(n))
            child = next(iter(provider._routed_providers.values()))
            events.extend(child._inflight.values())
        with entered:
            assert entered.wait_for(lambda: len(calls) == module.MAX_INFLIGHT_RECALLS, timeout=2)
        with host_context.bind(user='bob-im', chat='bob-chat'):
            provider.queue_prefetch('bob query')
            bob = provider._routed_provider('s1')
            assert not bob._inflight
        assert len(calls) == module.MAX_INFLIGHT_RECALLS
    finally:
        release.set()
        assert all(event.wait(3) for event in events)
    with host_context.bind(user='bob-im', chat='bob-chat'):
        provider.queue_prefetch('bob query after capacity released')
        event = next(iter(bob._inflight.values()), None)
        if event:
            assert event.wait(3)
    assert len(calls) == module.MAX_INFLIGHT_RECALLS + 1


def test_tool_result_is_not_returned_after_midflight_permission_removal(tmp_path, host_context, monkeypatch):
    ctx, policy, ids = prepare(tmp_path, host_context)
    plugin = load_file('identity_bridge_test', REPO / 'plugins/memory_bridge/__init__.py')
    plugin.register(ctx)
    def run(*args, **kwargs):
        policy.unlink()
        return types.SimpleNamespace(returncode=0, stdout='{"ok":true,"results":[{"statement":"private"}]}')
    monkeypatch.setattr(subprocess, 'run', run)
    with host_context.bind():
        result = json.loads(ctx.tools['memory_get_observations_l3']['handler']({'ids': [ids['alice']]}, session_id='s1'))
    assert not result['ok'] and 'private' not in json.dumps(result)


@pytest.mark.parametrize('revision', ['HEAD', 'bf0203a794a07aac3ff764972ab9087baa4e4f4e'])
def test_actual_host_identity_modules_and_progressive_tool(tmp_path, revision):
    interpreter = HOST / 'venv/bin/python'
    if not interpreter.is_file() or not (HOST / '.git').exists():
        pytest.skip('Hermes interpreter/source unavailable')
    staged = tmp_path / 'host'
    staged.mkdir()
    archive = subprocess.run(['git', '-C', str(HOST), 'archive', revision], capture_output=True, check=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as bundle:
        bundle.extractall(staged, filter='data')
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    cfg = types.SimpleNamespace(home=home)
    ctx, policy, ids = prepare(tmp_path, cfg)
    (home / 'config.yaml').write_text(json.dumps({'memory': {'provider': 'localvec', 'localvec': {
        'identity_routes_file': str(policy), 'engine_root': str(REPO), 'timeout_s': 2}}}))
    (home / 'plugins').mkdir()
    (home / 'plugins/localvec').symlink_to(REPO / 'plugins/localvec', target_is_directory=True)
    code = r'''
import importlib.util, json, pathlib, socket, sys
sys.path.insert(0, sys.argv[1])
from gateway import session_context as sc
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from agent.secret_scope import set_multiplex_active
from agent.delegation_context import delegated_child_context
from plugins.memory import find_provider_dir, load_memory_provider
def deny_network(*a, **k): raise AssertionError("offline identity test attempted network")
socket.socket.connect = deny_network
root, home, policy, own = sys.argv[2:]
spec = importlib.util.spec_from_file_location("real_memory_bridge", pathlib.Path(root)/"plugins/memory_bridge/__init__.py")
plugin = importlib.util.module_from_spec(spec); spec.loader.exec_module(plugin)
class Context:
    profile_name = "p1"
    tools, hooks = {}, {}
    settings = {"enabled":True, "engine_root":root, "identity_routes_file":policy}
    def get_config(self, key, default=None): return self.settings.get(key, default)
    def register_tool(self, **kw): self.tools[kw["name"]] = kw["handler"]
    def register_hook(self, name, fn): self.hooks[name] = fn
ctx = Context(); plugin.register(ctx)
handler = ctx.tools["memory_get_observations_l3"]
set_multiplex_active(True)
token = set_hermes_home_override(home)
sc.set_session_vars(platform="test-im", scope_id="workspace", user_id="alice-im", chat_id="alice-chat",
                    chat_type="dm", session_id="s1", session_key="route:s1", cron_session="")
result = json.loads(handler({"ids":[own]}, session_id="s1"))
assert result["ok"] and [r["fact_id"] for r in result["results"]] == [own], result
assert find_provider_dir('localvec').resolve() == pathlib.Path(root)/'plugins/localvec'
provider = load_memory_provider('localvec', register_skills=False)
assert provider is not None, 'host did not discover managed memory provider'
provider.initialize('s1')
# Exercise real host context propagation for automatic fallback; no embed/model.
import subprocess
original_run = subprocess.run
def unavailable(*a, **k):
    from types import SimpleNamespace
    return SimpleNamespace(returncode=1, stdout='')
subprocess.run = unavailable
try:
    text = provider.prefetch('private secret', session_id='s1')
    assert 'alice private secret' in text and 'bob private secret' not in text, text
finally:
    subprocess.run = original_run
ctx.hooks["post_llm_call"](session_id="s1", turn_id="host-turn", user_message="private", assistant_response="ack")
ctx.hooks["on_session_end"](session_id="s1", turn_id="host-turn", completed=True)
with delegated_child_context():
    assert json.loads(handler({"ids":[own]}, session_id="s1"))["ok"] is False
reset_hermes_home_override(token)
assert json.loads(handler({"ids":[own]}, session_id="s1"))["ok"] is False
# Actual host last-known-good is not authorization for a newly created provider.
token = set_hermes_home_override(home)
import os
os.environ["LOCALVEC_RECALL_CMD"] = "must-not-adopt-environment-owner"
pathlib.Path(home, "config.yaml").write_text("memory: [")
broken = type(provider)()
assert not broken.is_available()
broken.initialize("s1")
assert not broken._cmd and broken._config_failed
reset_hermes_home_override(token)
print("actual host scoped tool/capture/delegation/multiplex checks passed")
'''
    env = dict(os.environ, HOME=str(home), HERMES_HOME=str(home), PYTHONDONTWRITEBYTECODE='1',
               HERMES_DISABLE_LAZY_INSTALLS='1', HERMES_PM_EAGER_BOOTSTRAP='0')
    for key in tuple(env):
        if key.startswith(('FEISHU_', 'HFC_', 'LONGTASK_GUARD_', 'SUPERAGENT_HOST_TEST_', 'LOCALVEC_')):
            env.pop(key, None)
    result = subprocess.run([str(interpreter), '-c', code, str(staged), str(REPO),
                             str(home), str(policy), ids['alice']], cwd=tmp_path,
                            env=env, text=True, capture_output=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads(next((tmp_path / 'alice/spool').glob('*.json')).read_text())
    assert record['state'] == 'completed' and record['user_id'] == 'alice'
