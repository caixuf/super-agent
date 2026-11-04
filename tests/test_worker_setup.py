import json
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import venv

import yaml
import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "install/setup_worker.py"


def test_worker_follows_managed_release_after_switch(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("worker_setup", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    home = tmp_path / "profile"
    home.mkdir()
    current = tmp_path / "current"
    current.symlink_to(REPO, target_is_directory=True)
    config = {"plugins": {"enabled": ["durable-dispatch"], "entries": {
        "durable-dispatch": {"settings": {"enabled": True, "queue_root": str(tmp_path / "queue"),
            "allowed_roots": [str(tmp_path)], "engine_root": str(current)}}}}}
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    files = module.plan(home, REPO, tmp_path / "units", "demo", Path(sys.executable).absolute())
    runner = json.loads(files[home / "worker/runner.json"])
    assert runner[1] == str(current / "agentcore/analysis_runner.py")
    unit = files[tmp_path / "units/superagent-worker-demo.service"]
    assert "PYTHONPATH=" + str(current) in unit


def test_worker_setup_derives_policy_and_refuses_overwrite(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    source = tmp_path / "files"
    source.mkdir()
    queue = tmp_path / "queue"
    config = {"plugins": {"enabled": ["durable-dispatch"], "entries": {
        "durable-dispatch": {"settings": {"enabled": True, "queue_root": str(queue),
                                           "allowed_roots": [str(source)], "denied_globs": ["*.secret"]}}}}}
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    units = tmp_path / "units"
    command = [sys.executable, str(SCRIPT), "--profile-home", str(home), "--engine-root", str(REPO),
               "--profile", "canary", "--unit-dir", str(units)]
    dry = subprocess.run(command, capture_output=True, text=True)
    assert dry.returncode == 0, dry.stderr
    assert not units.exists() and not (home / "worker").exists()
    applied = subprocess.run(command + ["--apply"], capture_output=True, text=True)
    assert applied.returncode == 0, applied.stderr
    policy = json.loads((home / "worker/analysis-policy.json").read_text())
    runner = json.loads((home / "worker/runner.json").read_text())
    assert runner[0] == str(Path(sys.executable).absolute())
    assert policy["allowed_roots"] == [str(source)]
    assert policy["denied_globs"] == ["*.secret"]
    unit = (units / "superagent-worker-canary.service").read_text()
    assert str(queue) in unit and str(home) in unit
    if shutil.which("systemd-analyze"):
        runtime = tmp_path / "user-runtime"
        runtime.mkdir(mode=0o700)
        result = subprocess.run(
            ["systemd-analyze", "--user", "verify", str(units / "superagent-worker-canary.service")],
            env=dict(os.environ, XDG_RUNTIME_DIR=str(runtime)), capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    assert (home / "worker/runner.json").stat().st_mode & 0o077 == 0
    assert subprocess.run(command + ["--apply"], capture_output=True).returncode == 0
    (home / "worker/analysis-policy.json").write_text("custom")
    failed = subprocess.run(command + ["--apply"], capture_output=True)
    assert failed.returncode != 0
    assert (home / "worker/analysis-policy.json").read_text() == "custom"


def test_worker_setup_refuses_missing_authorized_roots(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text("plugins: {enabled: [durable-dispatch], entries: {durable-dispatch: {settings: {enabled: true, queue_root: /tmp/q}}}}")
    result = subprocess.run([sys.executable, str(SCRIPT), "--profile-home", str(home),
                             "--engine-root", str(REPO), "--profile", "p", "--apply",
                             "--unit-dir", str(tmp_path / "units")], capture_output=True)
    assert result.returncode != 0
    assert not (home / "worker").exists()


def test_worker_keeps_explicit_interpreter_symlink(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    config = {"plugins": {"enabled": ["durable-dispatch"], "entries": {
        "durable-dispatch": {"settings": {"enabled": True, "queue_root": str(tmp_path / "queue"),
                                           "allowed_roots": [str(source)]}}}}}
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    python = tmp_path / "runtime/bin/python"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(python.parents[1])
    result = subprocess.run([
        sys.executable, str(SCRIPT), "--profile-home", str(home), "--engine-root", str(REPO),
        "--profile", "p", "--python", str(python), "--unit-dir", str(tmp_path / "units"), "--apply",
    ], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    runner = json.loads((home / "worker/runner.json").read_text())
    assert runner[0] == str(python), "resolving a virtualenv symlink loses its environment"
    assert str(python) in (tmp_path / "units/superagent-worker-p.service").read_text()
    prefix = subprocess.check_output([runner[0], "-c", "import sys; print(sys.prefix)"], text=True)
    assert prefix.strip() == str(python.parents[1])


def test_activation_refuses_pending_queue_without_starting_service(tmp_path):
    import sqlite3
    home = tmp_path / "profile"
    home.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    queue = tmp_path / "queue"
    queue.mkdir()
    with sqlite3.connect(queue / "pool.db") as db:
        db.execute("CREATE TABLE jobs (status TEXT)")
        db.execute("INSERT INTO jobs VALUES ('queued')")
    config = {"plugins": {"enabled": ["durable-dispatch"], "entries": {
        "durable-dispatch": {"settings": {"enabled": True, "queue_root": str(queue),
                                           "allowed_roots": [str(source)]}}}}}
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    fakehome = tmp_path / "user"
    fakehome.mkdir()
    env = dict(os.environ, HOME=str(fakehome))
    result = subprocess.run([sys.executable, str(SCRIPT), "--profile-home", str(home),
                             "--engine-root", str(REPO), "--profile", "p", "--apply", "--activate"],
                            env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "nonterminal work" in result.stderr


@pytest.mark.parametrize("fresh", [False, True])
def test_installer_starts_configured_profile_worker_by_default(tmp_path, fresh):
    home = tmp_path / "user"
    profile = home / ".hermes/profiles/tester"
    profile.mkdir(parents=True)
    source = tmp_path / "source"
    source.mkdir()
    queue = tmp_path / "queue"
    config = {"plugins": {"enabled": ["durable-dispatch"], "entries": {
        "durable-dispatch": {"settings": {"enabled": True, "queue_root": str(queue),
                                           "allowed_roots": [str(source)]}}}}}
    if fresh:
        config = {"platform_toolsets": {"api_server": ["file"]}}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.headers["Authorization"] == "Bearer test-key"
            body = b'{"data":[{"id":"flash"},{"id":"main"}]}'
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    listener = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=listener.serve_forever, daemon=True)
    thread.start()
    config["providers"] = {"litellm": {"base_url": "http://127.0.0.1:%d/v1" % listener.server_port,
                                        "api_key": "test-key"}}
    (profile / "config.yaml").write_text(yaml.safe_dump(config))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    systemctl = bin_dir / "systemctl"
    systemctl.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$WORKER_TEST_CALLS"\n')
    systemctl.chmod(0o755)
    env = dict(os.environ, HOME=str(home), PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
               PLUGIN_ENABLE_MODE="merge", WORKER_TEST_CALLS=str(calls))
    env.pop("HERMES_HOME", None)
    command = ["bash", str(REPO / "install/install.sh"), "--profile", "tester", "--skip-patch", "--skip-deps"]
    if fresh:
        command.extend(["--analysis-root", str(source)])
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "enable --now superagent-worker-tester.service" in calls.read_text()
    assert (profile / "worker/runner.json").is_file()
    if fresh:
        installed = yaml.safe_load((profile / "config.yaml").read_text())
        assert "durable_dispatch" in installed["platform_toolsets"]["api_server"]
        assert installed["plugins"]["entries"]["durable-dispatch"]["settings"]["allowed_roots"] == [str(source)]
    assert (home / ".config/systemd/user/superagent-worker-tester.service").is_file()
    listener.shutdown()
    thread.join(timeout=2)
    listener.server_close()


def test_worker_uses_configured_engine_root(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    src = tmp_path / "source"
    src.mkdir()
    config = {"plugins": {"enabled": ["durable-dispatch"], "entries": {
        "durable-dispatch": {"settings": {"enabled": True, "queue_root": str(tmp_path / "queue"),
                                           "allowed_roots": [str(src)], "engine_root": str(REPO)}}}}}
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    unused_engine = tmp_path / "other-engine"
    result = subprocess.run([sys.executable, str(SCRIPT), "--profile-home", str(home),
                             "--engine-root", str(unused_engine), "--profile", "p", "--apply",
                             "--unit-dir", str(tmp_path / "units")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    runner = json.loads((home / "worker/runner.json").read_text())
    assert runner[1] == str(REPO / "agentcore/analysis_runner.py")


def test_model_preflight_refuses_missing_credentials_without_leaking_output(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("worker_setup", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text("providers: {}\n")
    fake = tmp_path / "systemctl"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    import pytest
    from unittest.mock import patch
    with patch.dict(os.environ, {"PATH": str(tmp_path), "HOME": str(tmp_path)}):
        with pytest.raises(ValueError, match="credentials, endpoint or flash/main routes unavailable"):
            module.preflight_model(home, REPO, Path(sys.executable))


def test_worker_plan_explicit_flowcoro_binds_exact_interpreter_and_directory(tmp_path, monkeypatch):
    import importlib.util
    import types
    spec = importlib.util.spec_from_file_location("setup_worker_flowcoro_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    home = tmp_path / "profile"
    home.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["durable-dispatch"],
        "entries": {"durable-dispatch": {"settings": {"enabled": True,
        "queue_root": str(tmp_path / "queue"), "allowed_roots": [str(source)]}}}}}))
    native = tmp_path / "native"
    native.mkdir()
    (native / "flowcoro_py.so").write_bytes(b"test placeholder")
    calls = []
    def checked(argv, **kwargs):
        calls.append((argv, kwargs))
        return types.SimpleNamespace(returncode=0)
    monkeypatch.setattr(module.subprocess, "run", checked)
    unitdir = tmp_path / "units"
    files = module.plan(home, REPO, unitdir, "p", Path(sys.executable),
                        backend="flowcoro", flowcoro_dir=native)
    unit = files[unitdir / "superagent-worker-p.service"]
    assert '"--backend" "flowcoro"' in unit
    assert 'PYTHONPATH=' + str(REPO) + ':' + str(native) in unit
    assert calls[0][0][0] == sys.executable and calls[0][0][1] == "-I"
    assert calls[0][0][-2:] == [str(native), str(native / "flowcoro_py.so")]
    with pytest.raises(ValueError, match="FlowCoro directory requires"):
        module.plan(home, REPO, unitdir, "p", Path(sys.executable),
                    backend="stdlib", flowcoro_dir=native)
    (native / "flowcoro_py.so").unlink()
    with pytest.raises(ValueError, match="binding missing"):
        module.plan(home, REPO, unitdir, "p", Path(sys.executable),
                    backend="flowcoro", flowcoro_dir=native)



_ABSENT = object()


def _concurrency_config(tmp_path, value=_ABSENT):
    home = tmp_path / "profile"
    home.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    settings = {"enabled": True, "queue_root": str(tmp_path / "queue"),
                "allowed_roots": [str(source)]}
    if value is not _ABSENT:
        settings["worker_concurrency"] = value
    config = {"plugins": {"enabled": ["durable-dispatch"], "entries": {
        "durable-dispatch": {"settings": settings}}}}
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    return home


def _unit_for(tmp_path, home):
    import importlib.util
    spec = importlib.util.spec_from_file_location("worker_setup_concurrency", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    files = module.plan(home, REPO, tmp_path / "units", "demo", Path(sys.executable).absolute())
    return files[tmp_path / "units/superagent-worker-demo.service"]


def test_worker_concurrency_follows_profile_setting(tmp_path):
    unit = _unit_for(tmp_path, _concurrency_config(tmp_path, 4))
    assert '"--workers" "4"' in unit


def test_worker_concurrency_defaults_to_one(tmp_path):
    unit = _unit_for(tmp_path, _concurrency_config(tmp_path))
    assert '"--workers" "1"' in unit


@pytest.mark.parametrize("bad", [0, -1, 17, True, "4", 1.5])
def test_worker_concurrency_rejects_out_of_policy(tmp_path, bad):
    with pytest.raises(ValueError, match="worker_concurrency"):
        _unit_for(tmp_path, _concurrency_config(tmp_path, bad))

