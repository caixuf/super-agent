import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import sys

import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "install/setup_memory.py"


def setup(home, units, *extra):
    return subprocess.run([sys.executable, str(SCRIPT), "--profile-home", str(home),
                           "--engine-root", str(REPO), "--profile", "p1", "--user", "u1",
                           "--agent", "a1", "--embed-url", "http://127.0.0.1:8910",
                           "--python", sys.executable, "--unit-dir", str(units), *extra],
                          capture_output=True, text=True, timeout=30)


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
        check = subprocess.run(["systemd-analyze", "--user", "verify", str(unit)], capture_output=True, text=True)
        if "Failed to connect to" not in check.stderr:
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
             "plugins/platforms/feishu/adapter.py"]
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
