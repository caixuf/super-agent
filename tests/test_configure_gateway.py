from pathlib import Path
import subprocess
import sys

import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "install/configure_gateway.py"


def call(path):
    return subprocess.run([sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True)


def test_adds_standalone_preserving_config_and_is_idempotent(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("# retained\nplugins:\n  enabled: []\n")
    assert call(config).returncode == 0
    text = config.read_text()
    assert "# retained" in text
    assert yaml.safe_load(text)["gateway"]["standalone"] is True
    assert call(config).returncode == 0
    assert config.read_text() == text


def test_existing_explicit_gateway_choice_is_preserved(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("gateway:\n  standalone: false\n")
    original = config.read_bytes()
    result = call(config)
    assert result.returncode != 0
    assert config.read_bytes() == original
