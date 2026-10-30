import json
import os
from pathlib import Path

import pytest

from agentcore.analysis_runner import run_analysis
from agentcore.colleague_evidence import HandoffEvidenceError, verify_internal_result
from agentcore.pool import Pool


def completed_job(tmp_path, *, origin=None):
    sources = tmp_path / 'sources'
    sources.mkdir()
    source = sources / 'data.txt'
    source.write_text('before\nobserved value is seven\nafter\n')
    pool = Pool(tmp_path / 'queue')
    job = pool.submit('internal-1', {'goal': 'Explain the observation', 'sources': [
        {'path': str(source), 'start_line': 2, 'end_line': 2}]}, replay_safe=True, origin=origin)
    row, fd = pool.claim()
    try:
        attempt = row['attempt']
        output = pool.root / 'attempts' / attempt

        def model(messages, **kwargs):
            return {'content': json.dumps({'conclusion': 'The observed value was seven.',
                'claims': [{'statement': 'Value seven is recorded.', 'evidence': [
                    {'source_id': 'source_1', 'line': 2, 'quote': 'value is seven'}]}],
                'limitations': ['No cause was established.']})}

        receipt = run_analysis({'job': job, 'attempt': attempt, 'payload': json.loads(row['payload'])},
                               {'allowed_roots': [str(sources)]}, model, output)
        assert pool.finish(job, attempt, True, receipt)
    finally:
        os.close(fd)
    return pool, job, attempt, output


def test_handoff_revalidates_identity_and_citations_without_exporting_sources(tmp_path):
    pool, job, attempt, output = completed_job(tmp_path)
    evidence = verify_internal_result(pool, job)
    assert evidence['attempt_id'] == attempt
    assert evidence['summary'] == 'The observed value was seven.'
    assert evidence['verification'] == 'citation_traceability_only'
    assert len(evidence['report_sha256']) == len(evidence['source_manifest_sha256']) == 64
    assert str(output) not in json.dumps(evidence)
    assert 'observed value is seven' not in json.dumps(evidence)


def test_handoff_rejects_modified_citation(tmp_path):
    pool, job, _, output = completed_job(tmp_path)
    report_path = output / 'report.json'
    report = json.loads(report_path.read_text())
    report['claims'][0]['evidence'][0]['quote'] = 'fabricated quote'
    report_path.write_text(json.dumps(report))
    with pytest.raises(HandoffEvidenceError, match='report_citations_invalid'):
        verify_internal_result(pool, job)


def test_handoff_rejects_different_source_manifest(tmp_path):
    pool, job, _, output = completed_job(tmp_path)
    path = output / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['sources'][0]['path'] = '/elsewhere/secret.txt'
    path.write_text(json.dumps(manifest))
    with pytest.raises(HandoffEvidenceError, match='snapshot_mismatch'):
        verify_internal_result(pool, job)


def test_handoff_rejects_artifact_symlink(tmp_path):
    pool, job, _, output = completed_job(tmp_path)
    path = output / 'report.json'
    contents = path.read_bytes()
    external = tmp_path / 'external.json'
    external.write_bytes(contents)
    path.unlink()
    path.symlink_to(external)
    with pytest.raises(HandoffEvidenceError):
        verify_internal_result(pool, job)


def test_handoff_refuses_user_addressed_job(tmp_path):
    pool, job, _, _ = completed_job(tmp_path, origin={'profile': 'p', 'session': 's', 'route': 'r'})
    with pytest.raises(HandoffEvidenceError, match='internal_completed_job_required'):
        verify_internal_result(pool, job)


def test_handoff_rejects_stale_attempt_identity(tmp_path):
    pool, job, _, output = completed_job(tmp_path)
    path = output / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['attempt'] = 'f' * 32
    path.write_text(json.dumps(manifest))
    with pytest.raises(HandoffEvidenceError, match='manifest_identity_mismatch'):
        verify_internal_result(pool, job)


def test_handoff_validates_dev_colleague_receipt(tmp_path):
    pool = Pool(tmp_path / 'dev_queue')
    job = pool.submit('dev-1', {'goal': 'Write test case', 'role': 'cases'}, replay_safe=True)
    row, fd = pool.claim()
    try:
        attempt = row['attempt']
        attempt_dir = pool.root / 'attempts' / attempt
        attempt_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            'job': job,
            'attempt': attempt,
            'role': 'cases',
            'verification': 'dev_traceability_v1',
            'worktree': '/tmp/mock-worktree',
            'modified_files': ['case_set/domain_a/wiper.dsl'],
            'artifacts': ['report.json', 'manifest.json', 'report.md'],
        }
        report = {
            'job': job,
            'attempt': attempt,
            'role': 'cases',
            'verification': 'dev_traceability_v1',
            'summary': 'Wrote 1 case and passed syntax check.',
            'success': True,
        }
        (attempt_dir / 'manifest.json').write_text(json.dumps(manifest))
        (attempt_dir / 'report.json').write_text(json.dumps(report))
        receipt = {
            'ok': True,
            'result': {
                'job': job,
                'attempt': attempt,
                'verification': 'dev_traceability_v1',
                'status': 'success',
                'summary': report['summary'],
                'worktree': manifest['worktree'],
                'artifacts': manifest['artifacts'],
            }
        }
        assert pool.finish(job, attempt, True, receipt)
    finally:
        os.close(fd)

    evidence = verify_internal_result(pool, job)
    assert evidence['attempt_id'] == attempt
    assert evidence['summary'] == 'Wrote 1 case and passed syntax check.'
    assert evidence['verification'] == 'dev_traceability_v1'
    assert evidence['modified_files'] == ['case_set/domain_a/wiper.dsl']
    assert evidence['success'] is True

