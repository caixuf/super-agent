import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agentcore.analysis_runner import AnalysisError, collect_sources, run_analysis


def case(tmp_path):
    sources = tmp_path / 'sources'
    sources.mkdir()
    log = sources / 'run.log'
    log.write_text('begin run\nERROR: expected 4, observed 7\nend run\n')
    job = {'job': 'job-1', 'attempt': 'attempt-1', 'payload': {
        'goal': 'Explain the mismatch', 'sources': [{'path': str(log), 'start_line': 2, 'end_line': 2}]}}
    return job, {'allowed_roots': [str(sources)]}, log


def report(messages):
    user = json.loads(messages[1]['content'])
    assert user['sources'][0]['lines'] == [{'line': 2, 'text': 'ERROR: expected 4, observed 7'}]
    return {'content': json.dumps({'conclusion': 'The log records an assertion mismatch.',
        'claims': [{'statement': 'Observed value differs from expected value.',
                    'evidence': [{'source_id': 'source_1', 'line': 2, 'quote': 'expected 4, observed 7'}]}],
        'limitations': ['The origin of the observed value is not established.']})}


def test_real_snapshot_artifacts_and_only_supplied_context(tmp_path):
    job, policy, log = case(tmp_path)
    before = log.read_bytes()
    result = run_analysis(job, policy, report, tmp_path / 'output')
    assert result['ok'] is True
    assert log.read_bytes() == before
    artifacts = [Path(p) for p in result['result']['artifacts']]
    assert all(p.is_file() for p in artifacts)
    manifest = json.loads((tmp_path / 'output/manifest.json').read_text())
    assert manifest['sources'][0]['sha256']
    assert manifest['sources'][0]['start_line'] == 2
    assert 'citation_traceability_only' == result['result']['verification']


@pytest.mark.parametrize('change,error', [
    ({'source_id': 'unknown'}, 'unverified_citation'),
    ({'line': 1}, 'unverified_citation'),
    ({'line': True}, 'unverified_citation'),
    ({'quote': 'invented observation'}, 'unverified_citation'),
    ({'quote': ''}, 'invalid_quote'),
])
def test_fabricated_or_out_of_range_citations_are_rejected(tmp_path, change, error):
    job, policy, _ = case(tmp_path)
    def bad(messages):
        value = json.loads(report(messages)['content'])
        value['claims'][0]['evidence'][0].update(change)
        return value
    with pytest.raises(AnalysisError, match=error):
        run_analysis(job, policy, bad, tmp_path / 'output')
    assert not (tmp_path / 'output/report.json').exists()


@pytest.mark.parametrize('value', ['not json', {'conclusion': 'Done', 'claims': []},
                                   {'conclusion': 'Done', 'claims': [{'statement': 'ok', 'evidence': []}]}])
def test_no_evidence_never_succeeds(tmp_path, value):
    job, policy, _ = case(tmp_path)
    with pytest.raises(AnalysisError):
        run_analysis(job, policy, lambda _: value, tmp_path / 'output')


def test_denies_escape_symlink_and_special_file(tmp_path):
    job, policy, log = case(tmp_path)
    outside = tmp_path / 'outside.txt'
    outside.write_text('private')
    link = log.parent / 'link.log'
    link.symlink_to(outside)
    ancestor = log.parent / 'directory'
    ancestor.symlink_to(tmp_path, target_is_directory=True)
    fifo = log.parent / 'fifo'
    os.mkfifo(fifo)
    for path in (outside, link, ancestor / 'outside.txt', fifo):
        with pytest.raises(AnalysisError):
            collect_sources([{'path': str(path)}], policy)


@pytest.mark.parametrize('name,content', [('.env', 'anything'), ('auth.json', '{}'),
    ('secret.pem', 'anything'), ('service.log', 'api_key=supersecretvalue'),
    ('settings.json', '{"password": "supersecretvalue"}'),
    ('request.log', 'Authorization: Bearer supersecretvalue')])
def test_sensitive_files_and_assignments_never_reach_model(tmp_path, name, content):
    job, policy, log = case(tmp_path)
    private = log.parent / name
    private.write_text(content)
    job['payload']['sources'] = [{'path': str(private)}]
    called = []
    with pytest.raises(AnalysisError):
        run_analysis(job, policy, lambda messages: called.append(messages))
    assert called == []


def test_file_total_and_line_budgets(tmp_path):
    job, policy, log = case(tmp_path)
    for overrides in ({'max_file_bytes': 3}, {'max_total_bytes': 3}, {'max_lines': 1}):
        sources = [{'path': str(log), 'start_line': 1, 'end_line': 2}]
        with pytest.raises(AnalysisError):
            collect_sources(sources, dict(policy, **overrides))
    with pytest.raises(AnalysisError):
        collect_sources([{'path': str(log)}, {'path': str(log)}], dict(policy, max_files=1))


def test_model_failure_is_redacted(tmp_path):
    job, policy, _ = case(tmp_path)
    def failing(messages):
        raise RuntimeError('credentials must not leak')
    with pytest.raises(AnalysisError, match='^model_call_failed$'):
        run_analysis(job, policy, failing)


def test_admin_model_options_forwarded_and_payload_cannot_override(tmp_path):
    job, policy, _ = case(tmp_path)
    options = {'timeout': 7, 'max_tokens': 123, 'retries': 0, 'thinking_max': False}
    policy['model_options'] = options
    job['payload']['model_options'] = {'timeout': 999, 'max_tokens': 999999}
    received = []
    def model(messages, **kwargs):
        received.append(kwargs)
        return report(messages)
    assert run_analysis(job, policy, model, tmp_path / 'output')['ok'] is True
    assert received == [options]


def test_explicit_fallback_retries_only_once_after_invalid_citation(tmp_path):
    job, policy, _ = case(tmp_path)
    policy['model_options'] = {'alias': 'flash'}
    policy['fallback_model_options'] = {'alias': 'main'}
    calls = []
    def model(messages, **options):
        calls.append(options['alias'])
        result = report(messages)
        if options['alias'] == 'flash':
            result['claims'][0]['evidence'][0]['quote'] = 'not present in the source'
        return result
    output = tmp_path / 'fallback-output'
    assert run_analysis(job, policy, model, output)['ok'] is True
    assert calls == ['flash', 'main']
    assert json.loads((output / 'manifest.json').read_text())['fallback_used'] is True


def test_fallback_not_selected_by_task_payload_or_repeated(tmp_path):
    job, policy, _ = case(tmp_path)
    job['payload']['fallback_model_options'] = {'alias': 'main'}
    calls = []
    def model(messages, **options):
        calls.append(options)
        raise RuntimeError('private detail')
    with pytest.raises(AnalysisError, match='^model_call_failed$'):
        run_analysis(job, policy, model, tmp_path / 'no-fallback')
    assert calls == [{}]
    policy['fallback_model_options'] = {'alias': 'main'}
    with pytest.raises(AnalysisError, match='^model_call_failed$'):
        run_analysis(job, policy, model, tmp_path / 'failed-fallback')
    assert len(calls) == 3


@pytest.mark.parametrize('options', [None, [], 'options', {1: 'not a string key'}])
def test_invalid_admin_model_options_fail_before_model_call(tmp_path, options):
    job, policy, _ = case(tmp_path)
    policy['model_options'] = options
    called = []
    with pytest.raises(AnalysisError, match='^invalid_model_options$'):
        run_analysis(job, policy, lambda messages: called.append(messages), tmp_path / 'output')
    assert called == []
    assert not (tmp_path / 'output').exists()


def test_cli_emits_one_receipt_and_keeps_diagnostics_private(tmp_path):
    job, policy, log = case(tmp_path)
    policy_path = tmp_path / 'policy.json'
    policy_path.write_text(json.dumps(policy))
    adapter = tmp_path / 'offline_adapter.py'
    adapter.write_text('''import json
def model(messages):
    return {'conclusion':'Mismatch recorded', 'claims':[{'statement':'Mismatch',
      'evidence':[{'source_id':'source_1','line':2,'quote':'observed 7'}]}], 'limitations':[]}
''')
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(tmp_path) + os.pathsep + str(root))
    result = subprocess.run([sys.executable, '-m', 'agentcore.analysis_runner', '--policy', str(policy_path),
        '--model-adapter', 'offline_adapter:model', '--output-dir', str(tmp_path / 'output')],
        input=json.dumps(job), text=True, capture_output=True, env=env, cwd=tmp_path, timeout=30)
    assert result.returncode == 0, result.stderr
    assert len(result.stdout.splitlines()) == 1
    assert json.loads(result.stdout)['ok'] is True
    job['payload']['sources'] = [{'path': str(log.parent / '.env')}]
    failure = subprocess.run([sys.executable, '-m', 'agentcore.analysis_runner', '--policy', str(policy_path),
        '--model-adapter', 'offline_adapter:model'], input=json.dumps(job), text=True,
        capture_output=True, env=env, cwd=tmp_path, timeout=30)
    assert failure.returncode == 1
    assert json.loads(failure.stdout) == {'ok': False, 'error': 'source_denied'}


def test_bounded_query_summary_preserves_complete_report(tmp_path):
    job, policy, _ = case(tmp_path)
    full = '故障记录显示数据不一致。' * 300
    def long_report(messages):
        value = json.loads(report(messages)['content'])
        value['conclusion'] = full
        return value
    receipt = run_analysis(job, policy, long_report, tmp_path / 'output')
    result = receipt['result']
    assert result['summary_truncated'] is True
    assert result['summary'] == full[:2000]
    assert json.loads((tmp_path / 'output/report.json').read_text())['conclusion'] == full
    assert result['verification'] == 'citation_traceability_only'
    assert 'causal' in result['verification_note']


def test_only_transmitted_source_window_is_screened_and_exposed(tmp_path):
    job, policy, log = case(tmp_path)
    private_marker = 'EXAMPLE_NOT_A_REAL_CREDENTIAL_123456'
    log.write_text(log.read_text() + 'api_key=' + private_marker + '\n')
    receipt = run_analysis(job, policy, report, tmp_path / 'output')
    assert receipt['ok'] is True
    manifest = json.loads((tmp_path / 'output/manifest.json').read_text())
    assert manifest['sources'][0]['lines'] == [{'line': 2, 'text': 'ERROR: expected 4, observed 7'}]
    assert private_marker not in (tmp_path / 'output/manifest.json').read_text()
    job['payload']['sources'][0].update(start_line=4, end_line=4)
    with pytest.raises(AnalysisError, match='source_contains_sensitive_assignment'):
        run_analysis(job, policy, report, tmp_path / 'rejected')
