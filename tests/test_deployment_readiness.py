import subprocess
import sys
from pathlib import Path

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
