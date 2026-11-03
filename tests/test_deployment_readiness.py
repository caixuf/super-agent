import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def test_readiness_reports_open_capabilities_without_revealing_secrets(tmp_path):
    (tmp_path / "config.yaml").write_text("""model:
  api_key: sample-private-value-should-not-print
plugins:
  enabled: [task-status, durable-dispatch]
  entries:
    task-status:
      settings:
        enabled: true
        engine_root: /somewhere
        liveness_db: /somewhere/tasks.db
    durable-dispatch:
      settings:
        enabled: true
        queue_root: /somewhere/queue
""")
    result = subprocess.run([sys.executable, str(REPO / "tools/deployment_readiness.py"),
                             "--profile-home", str(tmp_path), "--profile", "test"],
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert "[CONFIGURED] task-status" in result.stdout
    assert "[OPEN] durable-dispatch" in result.stdout
    assert "[OPEN] completion-delivery" in result.stdout
    assert "[OPEN] memory-ingestion-worker" in result.stdout
    assert "[OPEN] memory-store-alignment" in result.stdout
    assert "sample-private-value" not in result.stdout + result.stderr
    assert "not proof" in result.stdout


def test_worker_readiness_detects_policy_drift(tmp_path, monkeypatch):
    import importlib.util
    import json
    from types import SimpleNamespace
    import yaml
    spec = importlib.util.spec_from_file_location("readiness_test", REPO / "tools/deployment_readiness.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    home = tmp_path / "profile"
    home.mkdir()
    worker = home / "worker"
    worker.mkdir()
    engine = tmp_path / "engine"
    queue = tmp_path / "queue"
    config = {"plugins": {"entries": {"durable-dispatch": {"settings": {
        "engine_root": str(engine), "queue_root": str(queue), "allowed_roots": ["/approved"],
        "denied_globs": ["*.secret"]}}}}}
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    (worker / "analysis-policy.json").write_text(json.dumps({"allowed_roots": ["/approved"], "denied_globs": ["*.secret"]}))
    (worker / "runner.json").write_text(json.dumps(["python", str(engine / "agentcore/analysis_runner.py")]))
    units = tmp_path / ".config/systemd/user"
    units.mkdir(parents=True)
    (units / "superagent-worker-p.service").write_text('[Service]\nExecStart="/usr/bin/python3" "-m" "agentcore.pool" "--root" "' + str(queue) + '" "worker"\n')
    rows = {row[0]: row[1] for row in module.inspect(home, "p")}
    assert rows["durable-worker"] is True
    (worker / "analysis-policy.json").write_text(json.dumps({"allowed_roots": ["/stale"], "denied_globs": []}))
    rows = {row[0]: row[1] for row in module.inspect(home, "p")}
    assert rows["durable-worker"] is False


@pytest.fixture
def routed_readiness(tmp_path, monkeypatch):
    import importlib.util
    import json
    from types import SimpleNamespace
    import yaml

    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    monkeypatch.syspath_prepend(str(REPO / 'install'))
    setup = load('routed_readiness_setup', REPO / 'install/setup_memory.py')
    module = load('routed_readiness', REPO / 'tools/deployment_readiness.py')
    monkeypatch.setenv('HOME', str(tmp_path))
    home = tmp_path / 'profile'
    home.mkdir(mode=0o700)
    (home / 'plugins').mkdir()
    (home / 'plugins/localvec').symlink_to(REPO / 'plugins/localvec', target_is_directory=True)
    config = home / 'config.yaml'
    config.write_text('plugins:\n  enabled: [memory-bridge]\n')
    bindings = []
    for person in ('alice', 'bob'):
        state = tmp_path / person
        state.mkdir(mode=0o700)
        for part in ('data', 'spool'):
            (state / part).mkdir(mode=0o700)
        # The checker must not open a private database or create a queue.
        (state / 'data/coding_memory_cv.db').write_bytes(b'private-invalid-database')
        bindings.append(dict(platform='test-im', scope_id='workspace', user_id=person + '-im',
                             chat_id=person + '-chat', principal=person, agent_id='assistant',
                             state_dir=str(state)))
    bindings.append(dict(bindings[0], platform='other-im'))
    policy = home / 'memory-identities.json'
    policy.write_text(json.dumps(dict(version=1, profile='p1', profile_home=str(home), bindings=bindings)))
    policy.chmod(0o600)
    units = tmp_path / '.config/systemd/user'
    units.mkdir(parents=True)
    plans = []
    for person in ('alice', 'bob'):
        plan = setup.make_plan(home, REPO, 'p1', person, 'assistant', 'http://127.0.0.1:8910',
                               Path(sys.executable).absolute(), units, identity_routes_file=policy)
        config.write_text(plan['updated'])
        plan['unit_path'].write_text(plan['unit'])
        plans.append(plan)
    active = {p['unit_path'].name for p in plans}
    calls = []

    def systemctl(argv, **kwargs):
        assert argv[:4] == ['systemctl', '--user', 'is-active', '--quiet']
        assert 0 < kwargs['timeout'] <= 8
        calls.append(argv[-1])
        return SimpleNamespace(returncode=0 if argv[-1] in active else 3)

    monkeypatch.setattr(module.subprocess, 'run', systemctl)

    def rows():
        return {name: (ok, detail) for name, ok, detail in module.inspect(home, 'p1')}

    return SimpleNamespace(module=module, home=home, config=config, policy=policy,
                           plans=plans, active=active, calls=calls, rows=rows, yaml=yaml)


def test_readiness_verifies_all_routed_workers_without_reading_or_writing_memory(routed_readiness):
    fixture = routed_readiness
    before = {p.relative_to(fixture.home.parent): p.read_bytes()
              for p in fixture.home.parent.rglob('*') if p.is_file()}
    rows = fixture.rows()
    for name in ('memory-bridge', 'memory-identity-policy', 'memory-recall',
                 'memory-ingestion-worker', 'memory-store-alignment'):
        assert rows[name][0], (name, rows[name])
    assert all(fixture.calls.count(p['unit_path'].name) == 1 for p in fixture.plans)
    assert rows['memory-identity-policy'][1].startswith('2 private memory runtimes')
    assert before == {p.relative_to(fixture.home.parent): p.read_bytes()
                      for p in fixture.home.parent.rglob('*') if p.is_file()}
    assert 'private-invalid-database' not in str(rows)
    assert not any(person in str(rows) for person in ('alice', 'bob'))


@pytest.mark.parametrize('case', ['inactive', 'missing-unit', 'queue', 'principal', 'embed',
                                  'extra-command', 'duplicate-env', 'drop-in'])
def test_routed_readiness_does_not_accept_one_healthy_worker_for_all_principals(routed_readiness, case):
    fixture = routed_readiness
    plan = fixture.plans[1]
    unit = plan['unit_path']
    text = unit.read_text()
    if case == 'inactive':
        fixture.active.remove(unit.name)
    elif case == 'missing-unit':
        unit.unlink()
    elif case == 'drop-in':
        directory = unit.with_name(unit.name + '.d')
        directory.mkdir()
        (directory / 'override.conf').write_text('[Service]\nEnvironment=SUPERAGENT_MEMORY_USER=other\n')
    else:
        replacements = {
            'queue': (str(plan['state'] / 'events.db'), str(plan['state'] / 'wrong.db')),
            'principal': ('SUPERAGENT_MEMORY_USER=bob', 'SUPERAGENT_MEMORY_USER=wrong'),
            'embed': ('http://127.0.0.1:8910', 'http://127.0.0.1:8999'),
            'extra-command': ('[Service]', '[Service]\nExecStartPre=/bin/false'),
            'duplicate-env': ('[Service]', '[Service]\nEnvironment=SUPERAGENT_MEMORY_USER=bob'),
        }
        old, new = replacements[case]
        assert old in text
        unit.write_text(text.replace(old, new))
    rows = fixture.rows()
    assert not rows['memory-store-alignment'][0]
    if case in ('inactive', 'missing-unit'):
        assert not rows['memory-ingestion-worker'][0]


@pytest.mark.parametrize('case', ['null', 'missing', 'duplicate', 'public', 'profile', 'mismatch'])
def test_broken_routed_readiness_never_checks_legacy_memory_as_a_fallback(routed_readiness, case):
    import json
    fixture = routed_readiness
    config = fixture.yaml.safe_load(fixture.config.read_text())
    if case == 'null':
        config['memory']['localvec']['identity_routes_file'] = None
        fixture.config.write_text(fixture.yaml.safe_dump(config))
    elif case == 'mismatch':
        config['memory']['localvec']['embed_url'] = 'http://127.0.0.1:8999'
        fixture.config.write_text(fixture.yaml.safe_dump(config))
    elif case == 'missing':
        fixture.policy.unlink()
    elif case == 'public':
        fixture.policy.chmod(0o644)
    elif case == 'duplicate':
        fixture.policy.write_text('{"version":1,"version":1}')
    else:
        policy = json.loads(fixture.policy.read_text())
        policy['profile'] = 'wrong-profile'
        fixture.policy.write_text(json.dumps(policy))
    fixture.active.add('superagent-memory-p1.service')
    rows = fixture.rows()
    assert not rows['memory-identity-policy'][0]
    assert not rows['memory-recall'][0]
    assert not rows['memory-store-alignment'][0]
    assert not rows['memory-ingestion-worker'][0]
    assert 'superagent-memory-p1.service' not in fixture.calls


def test_routed_readiness_reports_active_legacy_ingestion_as_conflicting(routed_readiness):
    fixture = routed_readiness
    fixture.active.add('superagent-memory-p1.service')
    rows = fixture.rows()
    assert not rows['memory-ingestion-worker'][0]
    assert not rows['memory-store-alignment'][0]
    assert 'legacy' in rows['memory-ingestion-worker'][1]


def test_routed_readiness_status_query_failure_is_not_legacy_stopped(routed_readiness, monkeypatch):
    from types import SimpleNamespace
    fixture = routed_readiness
    original = fixture.module.subprocess.run

    def run(argv, **kwargs):
        if argv[-1] == 'superagent-memory-p1.service':
            return SimpleNamespace(returncode=1)
        return original(argv, **kwargs)

    monkeypatch.setattr(fixture.module.subprocess, 'run', run)
    rows = fixture.rows()
    assert not rows['memory-ingestion-worker'][0]
    assert not rows['memory-store-alignment'][0]


@pytest.mark.parametrize('text', [
    'memory: secret-invalid-value',
    'plugins:\n  entries: [secret-invalid-value]',
    'plugins:\n  entries:\n    memory-bridge:\n      settings: secret-invalid-value',
    'memory:\n  localvec: secret-invalid-value',
    'memory: {}\nmemory: {}',
])
def test_malformed_readiness_configuration_fails_without_secret_traceback(tmp_path, text):
    (tmp_path / 'config.yaml').write_text(text)
    result = subprocess.run([sys.executable, str(REPO / 'tools/deployment_readiness.py'),
                             '--profile-home', str(tmp_path), '--profile', 'p1'],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert 'readiness check failed:' in result.stderr
    assert 'secret-invalid-value' not in result.stdout + result.stderr
    assert 'Traceback' not in result.stderr
