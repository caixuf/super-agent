"""Persistent dispatch -> real subprocess runner -> checked artifact integration."""
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

from agentcore.worker import Worker


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('dispatch_pipeline_plugin', REPO / 'plugins/durable_dispatch/__init__.py')
PLUGIN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLUGIN)


@pytest.mark.parametrize('forged', [False, True], ids=['checked-evidence', 'forged-evidence'])
def test_persistent_dispatch_to_analysis_report(tmp_path, monkeypatch, forged):
    sources = tmp_path / 'sources'
    sources.mkdir()
    log = sources / 'failure.log'
    log.write_text('artifact integrity mismatch\nrunner aborted\n')
    service = PLUGIN.DispatchService(str(tmp_path / 'queue'), 'test-profile', [str(sources)])
    submitted = service.submit('session-a', {'request_key': 'event-1', 'goal': 'Analyze the failure',
                                            'sources': [{'path': str(log)}]})
    assert submitted['status'] == 'queued'
    # New service object represents a restarted caller. Queue and identity survive.
    restarted = PLUGIN.DispatchService(str(tmp_path / 'queue'), 'test-profile', [str(sources)])
    assert restarted.status('session-a', {'task_id': submitted['task_id']})['status'] == 'queued'
    with pytest.raises(PermissionError):
        restarted.status('session-b', {'task_id': submitted['task_id']})
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps({'allowed_roots': [str(sources)]}))
    quote = 'fabricated evidence' if forged else 'artifact integrity mismatch'
    report = {'conclusion': 'The log records an integrity mismatch.', 'claims': [
        {'statement': 'Integrity validation failed.', 'evidence': [
            {'source_id': 'source_1', 'line': 1, 'quote': quote}]}], 'limitations': ['Synthetic fixture.']}
    (tmp_path / 'pipeline_model.py').write_text('def analyze(messages):\n    return ' + repr(report) + '\n')
    monkeypatch.setenv('PYTHONPATH', os.pathsep.join([str(tmp_path), str(REPO), os.environ.get('PYTHONPATH', '')]))
    command = [sys.executable, str(REPO / 'agentcore/analysis_runner.py'), '--policy', str(policy),
               '--model-adapter', 'pipeline_model:analyze']
    stats = Worker(restarted.pool, command, timeout=30).run(drain=True)
    status = restarted.status('session-a', {'task_id': submitted['task_id']})
    assert status['status'] == ('failed' if forged else 'completed')
    assert stats['attempts_finished'] == (2 if forged else 1)
    receipt = restarted.pool.claim_receipt()
    assert receipt['receipt']['job'] == submitted['task_id']
    assert restarted.pool.acknowledge(submitted['task_id'], receipt['token'])
    assert restarted.pool.receipts() == []
    if not forged:
        result = status['result']['result']
        assert result['verification'] == 'citation_traceability_only'
        manifest = json.loads(Path(result['artifacts'][2]).read_text())
        assert manifest['sources'][0]['lines'][0]['text'] == 'artifact integrity mismatch'
        assert len(manifest['sources'][0]['sha256']) == 64
        assert '_dispatch_owner' not in json.dumps(manifest)
    else:
        assert not list((tmp_path / 'queue' / 'attempts').glob('*/report.json'))
