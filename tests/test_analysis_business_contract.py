"""Output variations seen with real business analysis, without weakening evidence."""
import json
import sys

import pytest

from agentcore.analysis_runner import AnalysisError, validate_report
from agentcore.pool import Pool


SNAPSHOTS = [{'source_id': 'source_1', 'lines': [{'line': 2, 'text': 'process alive'}]}]
REPORT = {'conclusion': 'The process is alive.', 'claims': [
    {'statement': 'The source states it is alive.', 'evidence': [
        {'source_id': 'source_1', 'line': 2, 'quote': 'process alive'}]}]}


def test_complete_fenced_json_still_checks_citations():
    text = '```json\n' + json.dumps(REPORT) + '\n```'
    assert validate_report({'content': text}, SNAPSHOTS)['claims']
    forged = text.replace('process alive', 'process dead')
    with pytest.raises(AnalysisError, match='unverified_citation'):
        validate_report({'content': forged}, SNAPSHOTS)


@pytest.mark.parametrize('text', ['prefix\n```json\n{}\n```', '```json\n{}',
                                  json.dumps(REPORT) + ' extra'])
def test_no_partial_or_prose_json_salvage(text):
    with pytest.raises(AnalysisError, match='model_invalid_json'):
        validate_report(text, SNAPSHOTS)


def test_truncation_rejected_even_if_visible_json_parses():
    with pytest.raises(AnalysisError, match='model_output_truncated'):
        validate_report({'content': json.dumps(REPORT), 'raw': {
            'choices': [{'finish_reason': 'length'}]}}, SNAPSHOTS)


@pytest.mark.parametrize('code,expected', [('model_invalid_json', 'model_invalid_json'),
                                         ('secret=must-not-leak', None)])
def test_failed_runner_retains_only_public_error_code(tmp_path, code, expected):
    pool = Pool(tmp_path / 'queue')
    job = pool.submit('business', {'goal': 'read-only'}, replay_safe=False)
    command = [sys.executable, '-c',
               'import json;print(json.dumps({"ok":False,"error":' + repr(code) + '}));raise SystemExit(1)']
    assert pool.run_once(command, timeout=10)
    status = pool.status(job)
    assert status['status'] == 'failed'
    result = json.loads(status['result'])
    assert result.get('runner_error') == expected
    assert 'must-not-leak' not in status['result']
