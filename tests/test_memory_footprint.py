"""内存占用报告必须只读、且如实反映向量被存了两份这件事。"""
import sqlite3

import pytest

from agentcore import memory_footprint


def _store(path, *, vectors=2, retired=0):
    conn = sqlite3.connect(path)
    try:
        conn.execute('CREATE TABLE working_fact (fact_id TEXT PRIMARY KEY, statement TEXT, status TEXT)')
        conn.execute('CREATE TABLE fact_embeddings (fact_id TEXT PRIMARY KEY, model TEXT, vector BLOB)')
        conn.execute('CREATE TABLE vec_facts (fact_id TEXT PRIMARY KEY, embedding BLOB)')
        for index in range(vectors):
            fid = 'f%d' % index
            conn.execute('INSERT INTO working_fact VALUES (?,?,?)', (fid, 's', 'active'))
            conn.execute('INSERT INTO fact_embeddings VALUES (?,?,?)', (fid, 'm', b'x' * 4096))
            conn.execute('INSERT INTO vec_facts VALUES (?,?)', (fid, b'x' * 4096))
        for index in range(retired):
            fid = 'r%d' % index
            conn.execute('INSERT INTO working_fact VALUES (?,?,?)', (fid, 's', 'deprecated'))
            conn.execute('INSERT INTO fact_embeddings VALUES (?,?,?)', (fid, 'm', b'x' * 4096))
        conn.commit()
    finally:
        conn.close()


def test_report_counts_facts_and_embeddings(tmp_path):
    path = tmp_path / 'store.db'
    _store(path, vectors=3)
    result = memory_footprint.report(path)
    assert result['fact_count'] == 3
    assert result['embedding_count'] == 3
    assert result['database_bytes'] == path.stat().st_size


def test_report_flags_duplicated_vector_storage(tmp_path):
    path = tmp_path / 'store.db'
    _store(path, vectors=3)
    result = memory_footprint.report(path)
    assert result['vector_storage_is_duplicated'] is True
    assert result['vector_bytes'] > 0


def test_report_does_not_modify_the_store(tmp_path):
    path = tmp_path / 'store.db'
    _store(path, vectors=2)
    before = path.read_bytes()
    memory_footprint.report(path)
    assert path.read_bytes() == before


def test_view_budget_fit_is_reported(tmp_path):
    path = tmp_path / 'store.db'
    _store(path, vectors=2)
    size = path.stat().st_size
    assert memory_footprint.report(path, view_budget_bytes=size * 2)['resident_view_fit'] == 'fits'
    assert memory_footprint.report(path, view_budget_bytes=size // 2)['resident_view_fit'] == 'exceeds'


def test_bucket_is_coarse_and_stable(tmp_path):
    path = tmp_path / 'store.db'
    _store(path, vectors=1)
    bucket = memory_footprint.report(path)['database_mib_bucket']
    assert bucket.startswith('<=') or bucket == '0'


def test_missing_database_is_reported_not_raised(tmp_path):
    code = memory_footprint.main(['--database', str(tmp_path / 'absent.db')])
    assert code == 1


def test_cli_emits_json(tmp_path, capsys):
    path = tmp_path / 'store.db'
    _store(path, vectors=2)
    assert memory_footprint.main(['--database', str(path), '--json']) == 0
    payload = capsys.readouterr().out
    assert '"fact_count": 2' in payload


def test_dormant_vectors_are_counted(tmp_path):
    """退役事实的向量必须被单独计数 —— 它们仍占着存储和常驻副本。"""
    path = tmp_path / 'store.db'
    _store(path, vectors=2, retired=3)
    result = memory_footprint.report(path)
    assert result['dormant_embedding_count'] == 3
    assert result['dormant_share_of_embeddings'] == 0.6
