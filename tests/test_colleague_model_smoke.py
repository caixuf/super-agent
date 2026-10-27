"""Synthetic offline tests only; no real provider is contacted."""
import json
import os
from pathlib import Path
import subprocess
import sys


SCRIPT = Path(__file__).resolve().parents[1] / 'tools' / 'colleague_model_smoke.py'


def _run(tmp_path, source, *, adapter='sample_adapter:ask', timeout=10):
    (tmp_path / 'sample_adapter.py').write_text(source, encoding='utf-8')
    env = os.environ.copy()
    env['PYTHONPATH'] = str(tmp_path) + os.pathsep + env.get('PYTHONPATH', '')
    return subprocess.run([sys.executable, str(SCRIPT), '--model-adapter', adapter, '--timeout', str(timeout)],
                          env=env, capture_output=True, text=True, timeout=20)


def test_fake_adapter_exercises_real_local_runtime_and_never_echoes_output(tmp_path):
    run = _run(tmp_path, '''
import sys
def ask(messages, **kwargs):
    print("PRIVATE_MODEL_OUTPUT")
    print("PRIVATE_ERROR_OUTPUT", file=sys.stderr)
    return {"conclusion": "value is 42", "claims": [{"statement": "returns 42", "evidence": [
        {"source_id": "source_1", "line": 2, "quote": "return 42"}]}], "limitations": []}
''')
    assert run.returncode == 0
    assert json.loads(run.stdout) == {'ok': True, 'timed_out': False, 'adapter_loaded': True,
        'model_called': True, 'report_validated': True, 'member_bound': True, 'signed_relay': True}
    assert 'PRIVATE_' not in run.stdout + run.stderr


def test_invalid_model_report_fails_without_exposing_reply(tmp_path):
    run = _run(tmp_path, '''
def ask(messages, **kwargs):
    return "PRIVATE_INVALID_RESPONSE"
''')
    result = json.loads(run.stdout)
    assert run.returncode == 1 and result['adapter_loaded'] and result['model_called']
    assert not result['ok'] and not result['report_validated'] and not result['signed_relay']
    assert 'PRIVATE_' not in run.stdout + run.stderr


def test_model_adapter_is_explicit_and_missing_adapter_fails_closed(tmp_path):
    missing = _run(tmp_path, 'def ask(*args, **kwargs): return {}', adapter='nonexistent_adapter:ask')
    assert missing.returncode == 1 and not json.loads(missing.stdout)['adapter_loaded']
    no_argument = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=10)
    assert no_argument.returncode == 2


def test_timeout_reports_only_booleans(tmp_path):
    run = _run(tmp_path, '''
import time
def ask(messages, **kwargs):
    time.sleep(10)
''', timeout=1)
    result = json.loads(run.stdout)
    assert run.returncode == 1 and result['timed_out'] and not result['ok']
    assert all(type(value) is bool for value in result.values())
