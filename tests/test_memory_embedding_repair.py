import pytest

from agentcore.memory import change_journal, embedding_repair, memory_store
from agentcore.memory.config import get_config
from agentcore.memory.read_view import MemoryReadView, ViewPending


def setup_pending(engine):
    result = engine.write_fact('原始记忆内容', subject='主题')
    engine.flush_background(timeout=30)
    conn = engine._connect()
    change_journal.install(conn)
    view = MemoryReadView(conn)
    seq = view.consumer.seq
    with conn:
        conn.execute('UPDATE fact_embeddings SET model=? WHERE fact_id=?', ('other-model', result['fact_id']))
    return conn, view, seq, result['fact_id']


def test_repair_dry_run_does_not_call_embedder_or_modify_data(engine, monkeypatch):
    conn, view, seq, fid = setup_pending(engine)
    try:
        monkeypatch.setattr(get_config(), 'embed_fn', lambda texts: pytest.fail('dry run called embedder'))
        out = embedding_repair.repair_pending_batch(after_seq=seq, user_id=get_config().default_user,
                                                    model=memory_store.DEFAULT_EMBED_MODEL)
        assert out['dry_run'] and out['candidate_count'] == 1 and out['repaired'] == 0
        assert conn.execute('SELECT model FROM fact_embeddings WHERE fact_id=?', (fid,)).fetchone()[0] == 'other-model'
    finally:
        view.close(); conn.close()


def test_repair_regenerates_vector_and_unblocks_view(engine):
    conn, view, seq, fid = setup_pending(engine)
    try:
        with pytest.raises(ViewPending):
            view.refresh()
        out = embedding_repair.repair_pending_batch(after_seq=seq, user_id=get_config().default_user,
                                                    model=memory_store.DEFAULT_EMBED_MODEL, apply=True)
        assert out['ok'] and out['repaired'] == 1
        assert conn.execute('SELECT model FROM fact_embeddings WHERE fact_id=?', (fid,)).fetchone()[0] == memory_store.DEFAULT_EMBED_MODEL
        view.refresh()
        assert view.ready
    finally:
        view.close(); conn.close()


def test_repair_discards_result_if_fact_changes_during_embedding(engine, monkeypatch):
    conn, view, seq, fid = setup_pending(engine)
    try:
        def update_then_embed(texts):
            with conn:
                conn.execute('UPDATE working_fact SET statement=? WHERE fact_id=?', ('修改后的内容', fid))
            return [[0.25] * get_config().embed_dim]
        monkeypatch.setattr(get_config(), 'embed_fn', update_then_embed)
        out = embedding_repair.repair_pending_batch(after_seq=seq, user_id=get_config().default_user,
                                                    model=memory_store.DEFAULT_EMBED_MODEL, apply=True)
        assert not out['ok'] and out['changed'] == 1 and out['repaired'] == 0
        assert conn.execute('SELECT model FROM fact_embeddings WHERE fact_id=?', (fid,)).fetchone()[0] == 'other-model'
    finally:
        view.close(); conn.close()


def test_repair_rejects_unverified_model_and_does_not_use_fallback(engine):
    conn, view, seq, _ = setup_pending(engine)
    try:
        with pytest.raises(ValueError, match='model mismatch'):
            embedding_repair.repair_pending_batch(after_seq=seq, user_id=get_config().default_user, model='other')
    finally:
        view.close(); conn.close()


def test_repair_primary_failure_does_not_use_fallback(engine, monkeypatch):
    conn, view, seq, fid = setup_pending(engine)
    try:
        monkeypatch.setattr(get_config(), 'embed_fn', lambda texts: None)
        monkeypatch.setattr(memory_store, '_embed_texts', lambda texts: pytest.fail('fallback used'))
        out = embedding_repair.repair_pending_batch(after_seq=seq, user_id=get_config().default_user,
                                                    model=memory_store.DEFAULT_EMBED_MODEL, apply=True)
        assert not out['ok'] and out['failed'] == 1 and out['repaired'] == 0
        assert conn.execute('SELECT model FROM fact_embeddings WHERE fact_id=?', (fid,)).fetchone()[0] == 'other-model'
    finally:
        view.close(); conn.close()


def make_checkpoint(path, view):
    from types import SimpleNamespace
    from agentcore.memory.recovery_checkpoint import RecoveryCheckpoint
    checkpoint = RecoveryCheckpoint(path, initialize=True)
    checkpoint.save(view.consumer, SimpleNamespace(end_seq=view.consumer.seq, end_token=view.consumer.token))
    return checkpoint


def test_repair_from_checkpoint_validates_and_recovers(engine, tmp_path):
    conn, view, seq, fid = setup_pending(engine)
    try:
        path = tmp_path / 'checkpoint.json'
        make_checkpoint(path, view)
        before = path.read_bytes()
        result = embedding_repair.repair_from_checkpoint(checkpoint_path=path,
            user_id=get_config().default_user, model=memory_store.DEFAULT_EMBED_MODEL, apply=True, archive_dir=tmp_path)
        assert result['ok'] and result['repaired'] == 1
        assert path.read_bytes() == before
        view.refresh()
        assert view.ready
    finally:
        view.close(); conn.close()


def test_repair_from_checkpoint_rejects_wrong_principal(engine, tmp_path):
    conn, view, seq, fid = setup_pending(engine)
    try:
        path = tmp_path / 'checkpoint.json'
        make_checkpoint(path, view)
        with pytest.raises(ValueError, match='principal'):
            embedding_repair.repair_from_checkpoint(checkpoint_path=path, user_id='another-user',
                                                    model=memory_store.DEFAULT_EMBED_MODEL, apply=True)
        assert conn.execute('SELECT model FROM fact_embeddings WHERE fact_id=?', (fid,)).fetchone()[0] == 'other-model'
    finally:
        view.close(); conn.close()


def test_repair_from_checkpoint_rejects_wrong_journal_anchor(engine, tmp_path):
    import json
    from agentcore.memory.read_view import ViewInvalid
    conn, view, seq, fid = setup_pending(engine)
    try:
        path = tmp_path / 'checkpoint.json'
        make_checkpoint(path, view)
        record = json.loads(path.read_text())
        record['token'] = '0' * 32
        path.write_text(json.dumps(record))
        with pytest.raises(ViewInvalid):
            embedding_repair.repair_from_checkpoint(checkpoint_path=path, user_id=get_config().default_user,
                                                    model=memory_store.DEFAULT_EMBED_MODEL, apply=True)
        assert conn.execute('SELECT model FROM fact_embeddings WHERE fact_id=?', (fid,)).fetchone()[0] == 'other-model'
    finally:
        view.close(); conn.close()


def test_cli_defaults_to_dry_run(engine, tmp_path, monkeypatch, capsys):
    import json
    import sys
    from types import SimpleNamespace
    conn, view, seq, fid = setup_pending(engine)
    try:
        path = tmp_path / 'checkpoint.json'
        make_checkpoint(path, view)
        monkeypatch.setitem(sys.modules, 'fake_memory_bootstrap', SimpleNamespace(configure=lambda: None))
        code = embedding_repair.main(['--bootstrap', 'fake_memory_bootstrap:configure',
            '--checkpoint', str(path), '--user', get_config().default_user,
            '--model', memory_store.DEFAULT_EMBED_MODEL])
        result = json.loads(capsys.readouterr().out)
        assert code == 0 and result['dry_run'] and result['candidate_count'] == 1
        assert conn.execute('SELECT model FROM fact_embeddings WHERE fact_id=?', (fid,)).fetchone()[0] == 'other-model'
    finally:
        view.close(); conn.close()


def test_repair_scans_multiple_batches_without_advancing_checkpoint(engine, tmp_path):
    ids = [engine.write_fact('第%d条受影响的记忆' % n, subject='批量恢复')['fact_id'] for n in range(7)]
    engine.flush_background(timeout=30)
    conn = engine._connect()
    change_journal.install(conn)
    view = MemoryReadView(conn)
    try:
        path = tmp_path / 'checkpoint.json'
        make_checkpoint(path, view)
        checkpoint_before = path.read_bytes()
        with conn:
            for fid in ids:
                conn.execute('UPDATE fact_embeddings SET model=? WHERE fact_id=?', ('other-model', fid))
        with pytest.raises(ViewPending):
            view.refresh()
        cursor = view.consumer.seq
        repaired = 0
        for _ in range(10):
            result = embedding_repair.repair_from_checkpoint(checkpoint_path=path,
                user_id=get_config().default_user, model=memory_store.DEFAULT_EMBED_MODEL,
                event_limit=3, max_facts=2, apply=True, scan_after_seq=cursor, archive_dir=tmp_path)
            repaired += result['repaired']
            if result['next_scan_after_seq'] is not None:
                cursor = result['next_scan_after_seq']
            if repaired == len(ids):
                break
        assert repaired == len(ids)
        assert path.read_bytes() == checkpoint_before
        view.refresh()
        assert view.ready
    finally:
        view.close(); conn.close()


def test_other_user_pending_fact_stops_scan_without_writing(engine, tmp_path):
    result = engine.write_fact('属于其他用户的记忆', subject='用户隔离', user_id='other-user')
    engine.flush_background(timeout=30)
    conn = engine._connect()
    change_journal.install(conn)
    view = MemoryReadView(conn)
    try:
        path = tmp_path / 'checkpoint.json'
        make_checkpoint(path, view)
        with conn:
            conn.execute('UPDATE fact_embeddings SET model=? WHERE fact_id=?', ('other-model', result['fact_id']))
        out = embedding_repair.repair_from_checkpoint(checkpoint_path=path,
            user_id=get_config().default_user, model=memory_store.DEFAULT_EMBED_MODEL, apply=True, archive_dir=tmp_path)
        assert not out['ok'] and out['other_user_candidates'] == 1
        assert out['next_scan_after_seq'] is None and out['repaired'] == 0
        assert conn.execute('SELECT model FROM fact_embeddings WHERE fact_id=?', (result['fact_id'],)).fetchone()[0] == 'other-model'
    finally:
        view.close(); conn.close()


def test_repair_refuses_to_consume_journal_reserve(engine, monkeypatch):
    conn, view, seq, fid = setup_pending(engine)
    try:
        with conn:
            conn.execute('UPDATE fact_embeddings SET model=? WHERE fact_id=?', ('other-model', fid))
            floor, latest = conn.execute('SELECT floor,latest FROM memory_view_meta').fetchone()
            conn.execute('UPDATE memory_view_meta SET max_events=?', (latest - floor + 2,))
        monkeypatch.setattr(get_config(), 'embed_fn', lambda texts: pytest.fail('capacity guard called embedder'))
        result = embedding_repair.repair_pending_batch(after_seq=seq, user_id=get_config().default_user,
            model=memory_store.DEFAULT_EMBED_MODEL, apply=True)
        assert not result['ok'] and result['error'] == 'journal_capacity_low'
        assert result['journal_remaining'] == 2 and result['next_scan_after_seq'] is None
        assert conn.execute('SELECT model FROM fact_embeddings WHERE fact_id=?', (fid,)).fetchone()[0] == 'other-model'
    finally:
        view.close(); conn.close()


def test_repair_does_not_overwrite_concurrent_embedding_update(engine, monkeypatch):
    conn, view, seq, fid = setup_pending(engine)
    try:
        new_blob = memory_store._pack_vector([0.75] * get_config().embed_dim)
        def update_then_embed(texts):
            with conn:
                conn.execute('UPDATE fact_embeddings SET model=?,vector=? WHERE fact_id=?',
                             ('newer-model', new_blob, fid))
            return [[0.25] * get_config().embed_dim]
        monkeypatch.setattr(get_config(), 'embed_fn', update_then_embed)
        out = embedding_repair.repair_pending_batch(after_seq=seq, user_id=get_config().default_user,
                                                    model=memory_store.DEFAULT_EMBED_MODEL, apply=True)
        row = conn.execute('SELECT model,vector FROM fact_embeddings WHERE fact_id=?', (fid,)).fetchone()
        assert not out['ok'] and out['repaired'] == 0
        assert tuple(row) == ('newer-model', new_blob)
    finally:
        view.close(); conn.close()


def test_checkpoint_repair_archives_original_before_writing(engine, tmp_path):
    import base64
    import json
    import stat
    conn, view, seq, fid = setup_pending(engine)
    try:
        path = tmp_path / 'checkpoint.json'
        make_checkpoint(path, view)
        old = conn.execute('SELECT model,vector FROM fact_embeddings WHERE fact_id=?', (fid,)).fetchone()
        result = embedding_repair.repair_from_checkpoint(checkpoint_path=path,
            user_id=get_config().default_user, model=memory_store.DEFAULT_EMBED_MODEL,
            apply=True, archive_dir=tmp_path)
        archive = __import__('pathlib').Path(result['archive'])
        payload = json.loads(archive.read_text())
        assert stat.S_IMODE(archive.stat().st_mode) == 0o600
        assert payload['entries'][0]['embedding']['model'] == old['model']
        assert base64.b64decode(payload['entries'][0]['embedding']['vector_b64']) == old['vector']
        assert '原始记忆内容' not in archive.read_text()
        assert result['repaired'] == 1
    finally:
        view.close(); conn.close()


def test_checkpoint_repair_requires_private_archive(engine, tmp_path):
    conn, view, seq, fid = setup_pending(engine)
    try:
        path = tmp_path / 'checkpoint.json'
        make_checkpoint(path, view)
        with pytest.raises(ValueError, match='archive'):
            embedding_repair.repair_from_checkpoint(checkpoint_path=path,
                user_id=get_config().default_user, model=memory_store.DEFAULT_EMBED_MODEL, apply=True)
        assert conn.execute('SELECT model FROM fact_embeddings WHERE fact_id=?', (fid,)).fetchone()[0] == 'other-model'
    finally:
        view.close(); conn.close()


def test_archive_failure_stops_before_embedding_or_writes(engine, tmp_path, monkeypatch):
    conn, view, seq, fid = setup_pending(engine)
    try:
        path = tmp_path / 'checkpoint.json'
        make_checkpoint(path, view)
        unsafe = tmp_path / 'not-private'
        unsafe.mkdir(mode=0o755)
        unsafe.chmod(0o755)
        monkeypatch.setattr(get_config(), 'embed_fn', lambda texts: pytest.fail('embedder called after archive failure'))
        with pytest.raises(ValueError, match='private'):
            embedding_repair.repair_from_checkpoint(checkpoint_path=path,
                user_id=get_config().default_user, model=memory_store.DEFAULT_EMBED_MODEL,
                apply=True, archive_dir=unsafe)
        assert conn.execute('SELECT model FROM fact_embeddings WHERE fact_id=?', (fid,)).fetchone()[0] == 'other-model'
    finally:
        view.close(); conn.close()


def test_archive_recovers_only_into_new_isolated_copy(engine, tmp_path):
    import sqlite3
    from agentcore.memory import embedding_recovery, vec_index
    conn, view, seq, fid = setup_pending(engine)
    try:
        path = tmp_path / 'checkpoint.json'
        make_checkpoint(path, view)
        before = tuple(conn.execute('SELECT model,vector,text_hash FROM fact_embeddings WHERE fact_id=?', (fid,)).fetchone())
        old_index = conn.execute('SELECT embedding FROM vec_facts WHERE fact_id=?', (fid,)).fetchone()[0]
        repaired = embedding_repair.repair_from_checkpoint(checkpoint_path=path,
            user_id=get_config().default_user, model=memory_store.DEFAULT_EMBED_MODEL,
            apply=True, archive_dir=tmp_path)
        assert repaired['repaired'] == 1
        source_path = engine.get_config().db_path()
        dest = tmp_path / 'recovery-copy.db'
        outcome = embedding_recovery.restore_to_new_copy(archive_path=repaired['archive'],
            source_path=source_path, destination_path=dest)
        assert outcome['restored'] == 1
        copy = sqlite3.connect(dest)
        try:
            assert vec_index.load_into(copy)
            assert copy.execute('SELECT model,vector,text_hash FROM fact_embeddings WHERE fact_id=?', (fid,)).fetchone() == before
            assert copy.execute('SELECT embedding FROM vec_facts WHERE fact_id=?', (fid,)).fetchone()[0] == old_index
        finally:
            copy.close()
        assert conn.execute('SELECT model FROM fact_embeddings WHERE fact_id=?', (fid,)).fetchone()[0] == memory_store.DEFAULT_EMBED_MODEL
        with pytest.raises(FileExistsError):
            embedding_recovery.restore_to_new_copy(archive_path=repaired['archive'],
                source_path=source_path, destination_path=source_path)
    finally:
        view.close(); conn.close()


def test_archive_recovery_rejects_changed_fact_and_removes_copy(engine, tmp_path):
    from agentcore.memory import embedding_recovery
    conn, view, seq, fid = setup_pending(engine)
    try:
        path = tmp_path / 'checkpoint.json'
        make_checkpoint(path, view)
        repaired = embedding_repair.repair_from_checkpoint(checkpoint_path=path,
            user_id=get_config().default_user, model=memory_store.DEFAULT_EMBED_MODEL,
            apply=True, archive_dir=tmp_path)
        with conn:
            conn.execute('UPDATE working_fact SET statement=? WHERE fact_id=?', ('已发生变化', fid))
        dest = tmp_path / 'recovery-copy.db'
        with pytest.raises(ValueError, match='fact changed'):
            embedding_recovery.restore_to_new_copy(archive_path=repaired['archive'],
                source_path=engine.get_config().db_path(), destination_path=dest)
        assert not dest.exists()
    finally:
        view.close(); conn.close()


def test_repair_preserves_concurrent_index_change(engine, tmp_path, monkeypatch):
    conn, view, seq, fid = setup_pending(engine)
    try:
        path = tmp_path / 'checkpoint.json'
        make_checkpoint(path, view)
        newer = memory_store._pack_vector([0.75] * get_config().embed_dim)
        def update_index_then_embed(texts):
            with conn:
                conn.execute('DELETE FROM vec_facts WHERE fact_id=?', (fid,))
                conn.execute('INSERT INTO vec_facts(fact_id,embedding) VALUES(?,?)', (fid, newer))
            return [[0.25] * get_config().embed_dim]
        monkeypatch.setattr(get_config(), 'embed_fn', update_index_then_embed)
        out = embedding_repair.repair_from_checkpoint(checkpoint_path=path,
            user_id=get_config().default_user, model=memory_store.DEFAULT_EMBED_MODEL,
            apply=True, archive_dir=tmp_path)
        assert not out['ok'] and out['repaired'] == 0
        assert conn.execute('SELECT embedding FROM vec_facts WHERE fact_id=?', (fid,)).fetchone()[0] == newer
        assert conn.execute('SELECT model FROM fact_embeddings WHERE fact_id=?', (fid,)).fetchone()[0] == 'other-model'
    finally:
        view.close(); conn.close()


def test_bounded_repair_cycle_recovers_multiple_pages(engine, tmp_path):
    ids = [engine.write_fact('批处理事实 %d' % n, subject='恢复')['fact_id'] for n in range(7)]
    engine.flush_background(timeout=30)
    conn = engine._connect()
    change_journal.install(conn)
    view = MemoryReadView(conn)
    try:
        path = tmp_path / 'checkpoint.json'
        make_checkpoint(path, view)
        checkpoint_before = path.read_bytes()
        with conn:
            for fid in ids:
                conn.execute('UPDATE fact_embeddings SET model=? WHERE fact_id=?', ('other-model', fid))
        out = embedding_repair.repair_cycle(checkpoint_path=path, user_id=get_config().default_user,
            model=memory_store.DEFAULT_EMBED_MODEL, archive_dir=tmp_path, event_limit=3,
            max_facts_per_batch=2, pause_seconds=0)
        assert out['ok'] and out['repaired'] == 7
        assert out['batches'] <= 8 and len(out['archives']) >= 4
        assert path.read_bytes() == checkpoint_before
        view.refresh()
        assert view.ready
    finally:
        view.close(); conn.close()


def test_repair_cycle_stops_after_backend_failure(engine, tmp_path, monkeypatch):
    conn, view, seq, fid = setup_pending(engine)
    try:
        path = tmp_path / 'checkpoint.json'
        make_checkpoint(path, view)
        calls = []
        def fail(texts):
            calls.append(1)
            return None
        monkeypatch.setattr(get_config(), 'embed_fn', fail)
        out = embedding_repair.repair_cycle(checkpoint_path=path, user_id=get_config().default_user,
            model=memory_store.DEFAULT_EMBED_MODEL, archive_dir=tmp_path, pause_seconds=0)
        assert not out['ok'] and out['batches'] == 1 and len(calls) == 1
        assert conn.execute('SELECT model FROM fact_embeddings WHERE fact_id=?', (fid,)).fetchone()[0] == 'other-model'
    finally:
        view.close(); conn.close()


def test_cycle_reports_already_committed_batches_on_later_error(engine, tmp_path, monkeypatch):
    ids = [engine.write_fact('异常处理事实 %d' % n, subject='恢复')['fact_id'] for n in range(2)]
    engine.flush_background(timeout=30)
    conn = engine._connect()
    change_journal.install(conn)
    view = MemoryReadView(conn)
    try:
        path = tmp_path / 'checkpoint.json'
        make_checkpoint(path, view)
        with conn:
            for fid in ids:
                conn.execute('UPDATE fact_embeddings SET model=? WHERE fact_id=?', ('other-model', fid))
        real = embedding_repair.repair_from_checkpoint
        calls = []
        def fail_second(**kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError('private backend detail')
            return real(**kwargs)
        monkeypatch.setattr(embedding_repair, 'repair_from_checkpoint', fail_second)
        out = embedding_repair.repair_cycle(checkpoint_path=path, user_id=get_config().default_user,
            model=memory_store.DEFAULT_EMBED_MODEL, archive_dir=tmp_path, max_facts_per_batch=1, pause_seconds=0)
        assert not out['ok'] and out['repaired'] == 1 and len(out['archives']) == 1
        assert out['stop_reason'] == 'batch_exception' and out['error_type'] == 'RuntimeError'
        assert 'private backend detail' not in str(out)
    finally:
        view.close(); conn.close()


def test_unattested_fallback_is_not_used_for_vectors(engine, monkeypatch):
    calls = []
    monkeypatch.setattr(get_config(), 'embed_fn', lambda texts: None)
    monkeypatch.setattr(get_config(), 'embed_fallbacks', (lambda texts: calls.append('fallback') or [[1.0] * get_config().embed_dim],))
    monkeypatch.setattr(memory_store, '_embed_via_ollama', lambda texts: calls.append('ollama') or [[1.0] * get_config().embed_dim])
    assert memory_store._embed_with_cache(['unattested-primary-failure-test']) is None
    assert calls == []


def test_host_can_explicitly_attest_fallback_model(engine, monkeypatch):
    calls = []
    monkeypatch.setattr(get_config(), 'embed_fn', lambda texts: None)
    monkeypatch.setattr(get_config(), 'embed_fallbacks_same_model', True)
    monkeypatch.setattr(memory_store, '_embed_via_ollama', lambda texts: None)
    monkeypatch.setattr(get_config(), 'embed_fallbacks', (lambda texts: calls.append(1) or [[1.0] * get_config().embed_dim],))
    result = memory_store._embed_with_cache(['explicit-compatible-fallback-test'])
    assert result == [[1.0] * get_config().embed_dim] and calls == [1]


def test_cache_does_not_reuse_vectors_after_configuration_change(engine):
    from dataclasses import replace
    from agentcore.memory.config import configure
    original = get_config()
    try:
        configure(replace(original, embed_fn=lambda texts: [[0.25] * original.embed_dim for _ in texts]))
        first = memory_store._embed_with_cache(['cache-configuration-change-test'])
        configure(replace(original, embed_fn=lambda texts: [[0.75] * original.embed_dim for _ in texts]))
        second = memory_store._embed_with_cache(['cache-configuration-change-test'])
        assert first[0][0] == 0.25 and second[0][0] == 0.75
    finally:
        configure(original)


def test_cache_drops_inflight_result_after_configuration_change(engine):
    from dataclasses import replace
    from agentcore.memory.config import configure
    original = get_config()
    replacement = replace(original, embed_fn=lambda texts: [[0.75] * original.embed_dim for _ in texts])
    def switch_during_call(texts):
        configure(replacement)
        return [[0.25] * original.embed_dim for _ in texts]
    try:
        configure(replace(original, embed_fn=switch_during_call))
        assert memory_store._embed_with_cache(['inflight-config-switch-test']) is None
        assert memory_store._embed_with_cache(['inflight-config-switch-test'])[0][0] == 0.75
    finally:
        configure(original)


def test_cache_invalidates_when_primary_changes_in_place(engine, monkeypatch):
    config = get_config()
    query = 'inplace-embedding-change-test'
    monkeypatch.setattr(config, 'embed_fn', lambda texts: [[0.25] * config.embed_dim for _ in texts])
    first = memory_store._embed_with_cache([query])
    monkeypatch.setattr(config, 'embed_fn', lambda texts: [[0.75] * config.embed_dim for _ in texts])
    second = memory_store._embed_with_cache([query])
    assert first[0][0] == 0.25 and second[0][0] == 0.75


def test_dry_run_never_opens_a_writable_engine_connection(engine, tmp_path, monkeypatch):
    conn, view, seq, fid = setup_pending(engine)
    try:
        path = tmp_path / 'checkpoint.json'
        make_checkpoint(path, view)
        monkeypatch.setattr(memory_store, '_connect', lambda: pytest.fail('opened writable engine connection'))
        out = embedding_repair.repair_from_checkpoint(checkpoint_path=path,
            user_id=get_config().default_user, model=memory_store.DEFAULT_EMBED_MODEL)
        assert out['dry_run'] and out['candidate_count'] == 1
    finally:
        view.close(); conn.close()
