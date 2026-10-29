"""Optional end-to-end Gateway test, using only a temporary home and loopback model."""
import os
from pathlib import Path
import site
import subprocess
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = os.environ.get('HERMES_SOURCE')


@pytest.mark.skipif(not SOURCE, reason='set HERMES_SOURCE to exercise the real Hermes Gateway')
def test_gateway_colleague_adapter_with_loopback_model():
    host = Path(SOURCE).resolve()
    python = host / 'venv/bin/python'
    upstream = host / 'tests/gateway/test_unified_gateway_native_live.py'
    if not python.is_file() or not upstream.is_file():
        pytest.skip('Hermes venv or native live-test fixture unavailable')
    with tempfile.TemporaryDirectory(prefix='colleague-gateway-host-') as tmp:
        home = Path(tmp) / 'home'
        home.mkdir(mode=0o700)
        env = os.environ.copy()
        env.update(HERMES_HOME=str(home), HERMES_SOURCE=str(host),
                   HERMES_DISABLE_LAZY_INSTALLS='1', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
                   PYTHONPATH=os.pathsep.join([str(host), str(ROOT), site.getusersitepackages()]))
        result = subprocess.run(
            [str(python), '-m', 'pytest', '--noconftest', '-q',
             str(ROOT / 'tests/fixtures/colleague_gateway_live_probe.py')],
            cwd=tmp, env=env, capture_output=True, text=True, timeout=180)
        assert result.returncode == 0, result.stdout + result.stderr
