import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import sys
import venv

import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "install/setup_memory.py"


def setup(home, units, *extra, python=sys.executable):
    return subprocess.run([sys.executable, str(SCRIPT), "--profile-home", str(home),
                           "--engine-root", str(REPO), "--profile", "p1", "--user", "u1",
                           "--agent", "a1", "--embed-url", "http://127.0.0.1:8910",
                           *(["--python", python] if python is not None else []),
                           "--unit-dir", str(units), *extra],
                          capture_output=True, text=True, timeout=30)


def test_memory_preserves_managed_engine_symlink(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text("plugins:\n  enabled: [memory-bridge]\n")
    engine = tmp_path / "current"
    engine.symlink_to(REPO, target_is_directory=True)
    units = tmp_path / "units"
    result = subprocess.run([sys.executable, str(SCRIPT), "--profile-home", str(home),
                             "--engine-root", str(engine), "--profile", "p1", "--user", "u1",
                             "--agent", "a1", "--embed-url", "http://127.0.0.1:8910",
                             "--unit-dir", str(units), "--apply"], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    cfg = yaml.safe_load((home / "config.yaml").read_text())
    assert cfg["plugins"]["entries"]["memory-bridge"]["settings"]["engine_root"] == str(engine)
    assert cfg["memory"]["localvec"]["recall_cmd"][1] == str(engine / "tools/recall_memory.py")
    assert "PYTHONPATH=" + str(engine) in (units / "superagent-memory-p1.service").read_text()


def test_memory_default_uses_invoking_interpreter(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text("plugins:\n  enabled: [memory-bridge]\n")
    units = tmp_path / "units"
    result = setup(home, units, "--apply", python=None)
    assert result.returncode == 0, result.stderr
    cfg = yaml.safe_load((home / "config.yaml").read_text())
    assert cfg["memory"]["localvec"]["recall_cmd"][0] == str(Path(sys.executable).absolute())
    assert str(Path(sys.executable).absolute()) in (units / "superagent-memory-p1.service").read_text()


def test_memory_keeps_explicit_interpreter_symlink(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text("plugins:\n  enabled: [memory-bridge]\n")
    python = tmp_path / "runtime/bin/python"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(python.parents[1])
    units = tmp_path / "units"
    result = setup(home, units, "--apply", python=str(python))
    assert result.returncode == 0, result.stderr
    cfg = yaml.safe_load((home / "config.yaml").read_text())
    assert cfg["memory"]["localvec"]["recall_cmd"][0] == str(python)
    assert str(python) in (units / "superagent-memory-p1.service").read_text()
    prefix = subprocess.check_output([str(python), "-c", "import sys; print(sys.prefix)"], text=True)
    assert prefix.strip() == str(python.parents[1])


def test_prepares_one_identity_store_and_is_idempotent(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    config = home / "config.yaml"
    config.write_text("# keep\nplugins:\n  enabled: [memory-bridge]\n")
    units = tmp_path / "units"
    assert setup(home, units).returncode == 0
    assert not (home / "state").exists()
    first = setup(home, units, "--apply")
    assert first.returncode == 0, first.stderr
    text = config.read_text()
    cfg = yaml.safe_load(text)
    bridge = cfg["plugins"]["entries"]["memory-bridge"]["settings"]
    recall = cfg["memory"]["localvec"]["recall_cmd"]
    assert bridge["user_id"] == "u1" and bridge["agent_id"] == "a1"
    assert bridge["spool_dir"] == str(home / "state/superagent-memory/spool")
    assert "--query-text={query}" in recall
    assert recall[recall.index("--data-dir") + 1] == str(home / "state/superagent-memory/data")
    assert (home / "state/superagent-memory/data/coding_memory_cv.db").is_file()
    unit = units / "superagent-memory-p1.service"
    assert unit.is_file()
    if shutil.which("systemd-analyze"):
        runtime = tmp_path / "user-runtime"
        runtime.mkdir(mode=0o700)
        check = subprocess.run(["systemd-analyze", "--user", "verify", str(unit)],
                               env=dict(os.environ, XDG_RUNTIME_DIR=str(runtime)),
                               capture_output=True, text=True)
        assert check.returncode == 0, check.stderr
    second = setup(home, units, "--apply")
    assert second.returncode == 0, second.stderr
    assert config.read_text() == text


def test_refuses_existing_memory_provider_without_mutation(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    config = home / "config.yaml"
    original = "plugins:\n  enabled: [memory-bridge]\nmemory:\n  provider: other\n"
    config.write_text(original)
    result = setup(home, tmp_path / "units", "--apply")
    assert result.returncode != 0
    assert config.read_text() == original
    assert not (home / "state").exists()


def test_activation_checks_model_and_embedding_and_starts_service(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"data": [{"id": "flash"}, {"id": "main"}]}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
        def do_POST(self):
            body = json.dumps({"dense": [[0.01] * 1024]}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        userhome = tmp_path / "user"
        userhome.mkdir()
        home = tmp_path / "profile"
        home.mkdir()
        cfg = {"plugins": {"enabled": ["memory-bridge"]},
               "providers": {"litellm": {"base_url": "http://127.0.0.1:%d/v1" % server.server_port,
                                           "api_key": "test-key"}}}
        (home / "config.yaml").write_text(yaml.safe_dump(cfg))
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        ctl = bin_dir / "systemctl"
        ctl.write_text('''#!/bin/sh
if [ "$2" = is-active ]; then test -f "$TEST_ACTIVE"; exit $?; fi
if [ "$2" = enable ]; then touch "$TEST_ACTIVE"; fi
exit 0
''')
        ctl.chmod(0o755)
        env = dict(os.environ, HOME=str(userhome), PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
                   TEST_ACTIVE=str(tmp_path / "active"))
        result = subprocess.run([sys.executable, str(SCRIPT), "--profile-home", str(home),
                                 "--engine-root", str(REPO), "--profile", "p1", "--user", "u1",
                                 "--agent", "a1", "--embed-url", "http://127.0.0.1:%d" % server.server_port,
                                 "--python", sys.executable, "--apply", "--activate"],
                                env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "active").exists()
        cfg = yaml.safe_load((home / "config.yaml").read_text())
        assert cfg["memory"]["localvec"]["user_id"] == "u1"
        assert cfg["memory"]["localvec"]["profile"] == "p1"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_installer_new_profile_configures_memory_by_default(tmp_path):
    import pytest
    host = Path.home() / ".hermes/hermes-agent"
    files = ["gateway/stream_consumer.py", "gateway/stream_consumer_transport.py",
             "gateway/stream_consumer_fallback.py", "gateway/run.py", "gateway/run_turn.py",
             "gateway/run_turn_progress.py", "gateway/run_busy.py", "gateway/platforms/base.py",
             "gateway/session_identity.py", "plugins/platforms/feishu/adapter.py"]
    if not all((host / rel).is_file() for rel in files):
        pytest.skip("Hermes source tree unavailable")
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"data":[{"id":"flash"},{"id":"main"}]}'
            self.send_response(200); self.end_headers(); self.wfile.write(body)
        def do_POST(self):
            body = json.dumps({"dense": [[0.01] * 1024]}).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        src = tmp_path / "hermes-source"
        for rel in files:
            target = src / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(host / rel, target)
        userhome = tmp_path / "user"
        userhome.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        hermes = bin_dir / "hermes"
        hermes.write_text('''#!/bin/sh
if [ "$1" = profile ] && [ "$2" = create ]; then
  mkdir -p "$HOME/.hermes/profiles/$3"
  printf 'providers:\n  litellm:\n    base_url: http://127.0.0.1:%s/v1\n    api_key: test-key\n' "$TEST_PORT" > "$HOME/.hermes/profiles/$3/config.yaml"
fi
exit 0
''')
        hermes.chmod(0o755)
        ctl = bin_dir / "systemctl"
        ctl.write_text('''#!/bin/sh
if [ "$2" = is-active ]; then test -f "$TEST_ACTIVE"; exit $?; fi
if [ "$2" = enable ]; then touch "$TEST_ACTIVE"; fi
exit 0
''')
        ctl.chmod(0o755)
        env = dict(os.environ, HOME=str(userhome), PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
                   TEST_ACTIVE=str(tmp_path / "active"), TEST_PORT=str(server.server_port),
                   HERMES_AGENT_DIR=str(src), PLUGIN_ENABLE_MODE="merge",
                   SUPERAGENT_MEMORY_EMBED_URL="http://127.0.0.1:%d" % server.server_port)
        env.pop("HERMES_HOME", None)
        result = subprocess.run(["bash", str(REPO / "install/install.sh"), "--profile", "newuser",
                                 "--skip-deps", "--skip-worker"],
                                env=env, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        home = userhome / ".hermes/profiles/newuser"
        data = yaml.safe_load((home / "config.yaml").read_text())
        assert data["memory"]["provider"] == "localvec"
        assert data["memory"]["localvec"]["profile"] == "newuser"
        assert (home / "state/superagent-memory/data/coding_memory_cv.db").is_file()
        assert (tmp_path / "active").exists()
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_readiness_detects_memory_identity_drift(tmp_path, monkeypatch):
    import importlib.util
    from types import SimpleNamespace
    userhome = tmp_path / "user"
    units = userhome / ".config/systemd/user"
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text("plugins:\n  enabled: [memory-bridge]\n")
    assert setup(home, units, "--apply").returncode == 0
    spec = importlib.util.spec_from_file_location("readiness_memory_test", REPO / "tools/deployment_readiness.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("HOME", str(userhome))
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    rows = {row[0]: row[1] for row in module.inspect(home, "p1")}
    assert rows["memory-store-alignment"] is True
    unit = units / "superagent-memory-p1.service"
    unit.write_text(unit.read_text().replace("SUPERAGENT_MEMORY_AGENT=a1", "SUPERAGENT_MEMORY_AGENT=other"))
    rows = {row[0]: row[1] for row in module.inspect(home, "p1")}
    assert rows["memory-store-alignment"] is False


def test_setup_memory_with_entity_db_generates_combined_in_process_recall_and_passes_readiness(tmp_path, monkeypatch):
    import importlib.util
    from types import SimpleNamespace
    userhome = tmp_path / "user"
    units = userhome / ".config/systemd/user"
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text("plugins:\n  enabled: [memory-bridge]\n")
    entity_db = tmp_path / "entity_rules.db"
    entity_db.write_bytes(b"")
    diag_file = tmp_path / "diag.json"

    res1 = setup(home, units, "--entity-db", str(entity_db), "--diagnostic-file", str(diag_file), "--apply")
    assert res1.returncode == 0, res1.stderr
    cfg = yaml.safe_load((home / "config.yaml").read_text())
    lv = cfg["memory"]["localvec"]
    assert lv["combined_entity_db"] == str(entity_db)
    assert lv["diagnostic_path"] == str(diag_file)
    assert lv["recall_cmd"][1] == str(REPO / "tools/recall_combined.py")
    assert "--in-process" in lv["recall_cmd"]
    assert lv["recall_cmd"][lv["recall_cmd"].index("--entity-db") + 1] == str(entity_db)
    assert lv["recall_cmd"][lv["recall_cmd"].index("--diagnostic-file") + 1] == str(diag_file)

    # Idempotent re-run even when --entity-db is omitted on subsequent invocation
    saved_text = (home / "config.yaml").read_text()
    res2 = setup(home, units, "--apply")
    assert res2.returncode == 0, res2.stderr
    assert (home / "config.yaml").read_text() == saved_text

    # Deployment readiness check verifies store alignment with recall_combined.py
    spec = importlib.util.spec_from_file_location("readiness_combined_test", REPO / "tools/deployment_readiness.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("HOME", str(userhome))
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    rows = {row[0]: row[1] for row in module.inspect(home, "p1")}
    assert rows["memory-recall"] is True
    assert rows["memory-store-alignment"] is True

