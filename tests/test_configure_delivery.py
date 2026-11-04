import json
from pathlib import Path
import subprocess
import sys
import time

import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "install/configure_delivery.py"


def run(config):
    return subprocess.run([sys.executable, str(SCRIPT), str(config), str(REPO)],
                          capture_output=True, text=True)


def test_new_delivery_defaults_are_fenced_and_idempotent(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("""# keep
plugins:
  entries:
    durable-dispatch:
      settings:
        enabled: true
        engine_root: """ + str(REPO) + """
""")
    before = time.time()
    result = run(config)
    assert result.returncode == 0, result.stderr
    text = config.read_text()
    settings = yaml.safe_load(text)["plugins"]["entries"]["durable-dispatch"]["settings"]
    assert settings["deliver_completions"] is True
    assert settings["direct_delivery_enabled"] is True
    assert settings["delivery_not_before"] >= before
    assert settings["delivery_transport_file"] == str(REPO / "runtime/hermes_completion_transport.py")
    assert "# keep" in text
    assert run(config).returncode == 0
    assert config.read_text() == text


def test_explicit_delivery_policy_is_not_changed(tmp_path):
    for value in ("false", "true"):
        config = tmp_path / (value + ".yaml")
        original = "plugins:\n  entries:\n    durable-dispatch:\n      settings:\n        enabled: true\n        deliver_completions: " + value + "\n"
        config.write_text(original)
        assert run(config).returncode == 0
        assert config.read_text() == original


def test_conflicting_transport_is_rejected_without_edit(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("plugins:\n  entries:\n    durable-dispatch:\n      settings:\n        enabled: true\n        delivery_transport_file: /other/transport.py\n")
    original = config.read_bytes()
    result = run(config)
    assert result.returncode != 0
    assert config.read_bytes() == original


def test_old_cutoff_does_not_activate_historical_delivery(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("plugins:\n  entries:\n    durable-dispatch:\n      settings:\n        enabled: true\n        delivery_not_before: 0\n")
    before = config.read_bytes()
    result = run(config)
    assert result.returncode != 0
    assert config.read_bytes() == before
