import sqlite3

import pytest

from agentcore.memory import change_journal as journal,memory_store as store
from agentcore.memory.read_view import MemoryReadView,ViewPending,ViewInvalid





@pytest.mark.parametrize('bad', ['dimension', 'nan', 'infinite', 'overflow'])
def test_invalid_background_vector_does_not_replace_valid_embedding(engine, bad):
    fid = seed(engine)
    conn = engine._connect()
    try:
        before = conn.execute('SELECT vector,text_hash FROM fact_embeddings WHERE fact_id=?', (fid,)).fetchone()
        vec = [0.25] * engine.get_config().embed_dim
        if bad == 'dimension':
            vec.pop()
        elif bad == 'nan':
            vec[0] = float('nan')
        elif bad == 'infinite':
            vec[0] = float('inf')
        else:
            vec[0] = 1e300
        store._persist_fact_embedding(fid, '运行预算 并发预算固定为七个，后台任务不能阻塞主对话', vec)
        after = conn.execute('SELECT vector,text_hash FROM fact_embeddings WHERE fact_id=?', (fid,)).fetchone()
        assert tuple(after) == tuple(before)
    finally:
        conn.close()


def test_invalid_embedding_is_not_cached(engine, monkeypatch):
    query = 'invalid-vector-not-cached-20261030'
    calls = []
    def invalid(texts):
        calls.append(texts)
        return [[float('nan')] * engine.get_config().embed_dim]
    monkeypatch.setattr(store, '_embed_texts', invalid)
    assert store._embed_with_cache([query]) is None
    assert store._embed_with_cache([query]) is None
    assert len(calls) == 2


def test_failed_background_index_write_does_not_commit_new_embedding(engine, monkeypatch):
    from agentcore.memory import vec_index
    fid = seed(engine)
    conn = engine._connect()
    try:
        if not vec_index.load_into(conn):
            pytest.skip('sqlite-vec extension unavailable')
        before = conn.execute('SELECT vector,text_hash FROM fact_embeddings WHERE fact_id=?', (fid,)).fetchone()
        old_index = conn.execute('SELECT embedding FROM vec_facts WHERE fact_id=?', (fid,)).fetchone()[0]
        connect = store._connect
        class FailingIndexConnection:
            def __init__(self, inner):
                self.inner = inner
            def execute(self, sql, *args):
                if sql.startswith('INSERT INTO vec_facts'):
                    raise sqlite3.OperationalError('simulated index failure')
                return self.inner.execute(sql, *args)
            def __getattr__(self, name):
                return getattr(self.inner, name)
        monkeypatch.setattr(store, '_connect', lambda: FailingIndexConnection(connect()))
        text = '运行预算 并发预算固定为七个，后台任务不能阻塞主对话'
        store._persist_fact_embedding(fid, text, [0.25] * engine.get_config().embed_dim)
        after = conn.execute('SELECT vector,text_hash FROM fact_embeddings WHERE fact_id=?', (fid,)).fetchone()
        assert tuple(after) == tuple(before)
        assert conn.execute('SELECT embedding FROM vec_facts WHERE fact_id=?', (fid,)).fetchone()[0] == old_index
    finally:
        conn.close()


@pytest.mark.parametrize('change', ['corrected', 'retired'])
def test_background_embedding_discards_result_when_fact_changes(engine, monkeypatch, change):
    fid = seed(engine)
    conn = engine._connect()
    try:
        before = conn.execute('SELECT model,vector,text_hash FROM fact_embeddings WHERE fact_id=?', (fid,)).fetchone()
        def embed(texts):
            with conn:
                if change == 'corrected':
                    conn.execute('UPDATE working_fact SET statement=? WHERE fact_id=?', ('新事实', fid))
                else:
                    conn.execute("UPDATE working_fact SET status='deprecated' WHERE fact_id=?", (fid,))
            return [[0.25] * engine.get_config().embed_dim]
        monkeypatch.setattr(store, '_embed_with_cache', embed)
        store._bg_embed(fid, '运行预算', '并发预算固定为七个，后台任务不能阻塞主对话')
        after = conn.execute('SELECT model,vector,text_hash FROM fact_embeddings WHERE fact_id=?', (fid,)).fetchone()
        assert tuple(after) == tuple(before)
    finally:
        conn.close()


def test_background_embedding_indexes_candidate_fact(engine):
    result = engine.write_fact('候选事实需要向量', subject='候选', initial_status='candidate',
                               resolve_conflict=False, dedupe_by_statement=False)
    engine.flush_background(timeout=30)
    conn = engine._connect()
    try:
        row = conn.execute('SELECT text_hash FROM fact_embeddings WHERE fact_id=?', (result['fact_id'],)).fetchone()
        assert row['text_hash'] == store._text_hash('候选 候选事实需要向量')
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='vec_facts'").fetchone():
            assert conn.execute('SELECT 1 FROM vec_facts WHERE fact_id=?', (result['fact_id'],)).fetchone()
    finally:
        conn.close()


def test_reindex_rejects_unverified_model_before_read_or_write(monkeypatch):
    from agentcore.memory import memory_store
    def unexpected():
        pytest.fail('mismatched model must not access the database')
    monkeypatch.setattr(memory_store, '_connect', unexpected)
    result = memory_store.reindex_embeddings(model=memory_store.DEFAULT_EMBED_MODEL + '-other')
    assert result == {'ok': False, 'error': 'embedding_model_mismatch'}


def test_reindex_skips_fact_corrected_during_embedding(engine, monkeypatch):
    fid = seed(engine)
    conn = engine._connect()
    try:
        before = conn.execute('SELECT model,vector,text_hash FROM fact_embeddings WHERE fact_id=?', (fid,)).fetchone()
        def embed(texts):
            with conn:
                conn.execute('UPDATE working_fact SET statement=? WHERE fact_id=?', ('已修正的新事实', fid))
            return [[0.25] * engine.get_config().embed_dim for _ in texts]
        monkeypatch.setattr(store, '_embed_with_cache', embed)
        result = store.reindex_embeddings(force=True)
        after = conn.execute('SELECT model,vector,text_hash FROM fact_embeddings WHERE fact_id=?', (fid,)).fetchone()
        assert not result['ok'] and result['error'] == 'facts_changed_during_embedding'
        assert result['updated'] == 0 and result['skipped_changed'] == 1
        assert tuple(after) == tuple(before)
    finally:
        conn.close()



def test_reindex_reports_partial_commit_when_only_one_fact_changes(engine, monkeypatch):
    first = seed(engine)
    second = engine.write_fact('新会话需要自动召回有效记忆', subject='召回规则')['fact_id']
    engine.flush_background(timeout=30)
    conn = engine._connect()
    try:
        def embed(texts):
            with conn:
                conn.execute('UPDATE working_fact SET statement=? WHERE fact_id=?', ('已修正的新事实', first))
            return [[0.25] * engine.get_config().embed_dim for _ in texts]
        monkeypatch.setattr(store, '_embed_with_cache', embed)
        result = store.reindex_embeddings(force=True)
        assert not result['ok']
        assert result['error'] == 'facts_changed_during_embedding'
        assert result['updated'] == 1 and result['skipped_changed'] == 1
        row = conn.execute('SELECT text_hash FROM fact_embeddings WHERE fact_id=?', (second,)).fetchone()
        assert row['text_hash'] == store._text_hash('召回规则 新会话需要自动召回有效记忆')
    finally:
        conn.close()



def test_reindex_reports_index_race_after_embedding_commit(engine, monkeypatch):
    from agentcore.memory import vec_index
    seed(engine)
    monkeypatch.setattr(store, '_embed_with_cache',
                        lambda texts: [[0.25] * engine.get_config().embed_dim for _ in texts])
    monkeypatch.setattr(vec_index, 'upsert_current', lambda *args, **kwargs:
                        {'available': True, 'indexed': 0, 'skipped_changed': 1, 'failed': 0})
    result = store.reindex_embeddings(force=True)
    assert result['updated'] == 1
    assert result['ok'] is False and result['error'] == 'vector_index_incomplete'
    assert result['index_skipped_changed'] == 1


def test_reindex_still_updates_unchanged_fact(engine, monkeypatch):
    fid = seed(engine)
    monkeypatch.setattr(store, '_embed_with_cache',
                        lambda texts: [[0.25] * engine.get_config().embed_dim for _ in texts])
    result = store.reindex_embeddings(force=True)
    assert result['ok'] and result['updated'] == 1 and result['skipped_changed'] == 0
    conn = engine._connect()
    try:
        row = conn.execute('SELECT model,dim,text_hash FROM fact_embeddings WHERE fact_id=?', (fid,)).fetchone()
        assert row['model'] == store.DEFAULT_EMBED_MODEL
        assert row['dim'] == engine.get_config().embed_dim
        assert row['text_hash'] == store._text_hash('运行预算 并发预算固定为七个，后台任务不能阻塞主对话')
    finally:
        conn.close()


def seed(engine):
    result=engine.write_fact('并发预算固定为七个，后台任务不能阻塞主对话',subject='运行预算')
    engine.flush_background(timeout=30)
    return result['fact_id']


def correct(conn,fid,subject,text,scope='ops'):
    with conn:
        conn.execute('UPDATE working_fact SET statement=? WHERE fact_id=?',(text,fid))
        conn.execute("UPDATE fact_tags SET value=? WHERE fact_id=? AND key='scope'",(scope,fid))
        vector=store._embed_with_cache([subject+' '+text])[0]
        conn.execute('UPDATE fact_embeddings SET vector=?,text_hash=? WHERE fact_id=?',
                     (store._pack_vector(vector),store._text_hash(subject+' '+text),fid))


def test_reuses_exact_engine_and_applies_correction_retirement_and_delete(engine):
    fid=seed(engine);source=engine._connect();journal.install(source)
    view=MemoryReadView(source)
    try:
        assert view.ready and view.recall('后台任务会阻塞主对话吗')==engine.recall_facts('后台任务会阻塞主对话吗')
        correct(source,fid,'运行预算','并发预算已改为九个，后台工作不能占用主回合')
        current=view.recall('运行预算',scopes=['ops'])
        assert current==engine.recall_facts('运行预算',scopes=['ops'])
        assert current['results'][0]['tags']['scope']=='ops' and '九个' in current['results'][0]['statement']
        engine.drop_fact(fid,reason='test_revoked')
        assert not view.recall('运行预算')['results']
        with source:
            for table in ('fact_tags','fact_embeddings','working_fact'):source.execute('DELETE FROM '+table+' WHERE fact_id=?',(fid,))
        view.refresh()
        assert not view.conn.execute('SELECT 1 FROM working_fact WHERE fact_id=?',(fid,)).fetchone()
        if view.conn.execute("SELECT 1 FROM sqlite_master WHERE name='vec_facts'").fetchone():
            assert not view.conn.execute('SELECT 1 FROM vec_facts WHERE fact_id=?',(fid,)).fetchone()
    finally:view.close();source.close()


def test_online_backup_pins_snapshot_and_then_applies_writer_during_copy(engine):
    seed(engine);source=engine._connect()
    source.execute('CREATE TABLE snapshot_padding(payload BLOB)')
    source.execute('INSERT INTO snapshot_padding VALUES (zeroblob(2097152))');source.commit()
    journal.install(source);written=[]
    def copying(status,remaining,total):
        if remaining and not written:
            result=engine.write_fact('复制期间已经提交的新用户约定',subject='交接演练')
            engine.flush_background(timeout=30);written.append(result['fact_id'])
    view=MemoryReadView(source,max_bytes=16*1024*1024,progress=copying)
    try:
        assert written and view.ready
        assert view.recall('交接演练')['results'][0]['fact_id']==written[0]
        assert view.conn.execute('SELECT length(payload) FROM snapshot_padding').fetchone()[0]==2097152
        assert not view.conn.execute("SELECT 1 FROM sqlite_master WHERE name='memory_view_meta'").fetchone()
    finally:view.close();source.close()


def test_new_fact_waits_for_embedding_without_acknowledging_incomplete_view(engine):
    seed(engine);source=engine._connect();journal.install(source);view=MemoryReadView(source)
    try:
        before=view.consumer.seq
        with source:
            source.execute("INSERT INTO working_fact(fact_id,user_id,subject,statement,created_at) VALUES ('new',?,'新约定','先完成任务再优化','2026')",
                           (store.get_config().default_user,))
        with pytest.raises(ViewPending):view.recall('新约定')
        assert not view.ready and not view.invalid and view.consumer.seq==before
        text='新约定 先完成任务再优化';vector=store._embed_with_cache([text])[0]
        store._persist_fact_embedding('new',text,vector)
        assert view.recall('新约定')['results'][0]['fact_id']=='new'
    finally:view.close();source.close()


def test_gap_withdraws_readiness_and_repair_does_not_silently_reuse_invalid_view(engine):
    fid=seed(engine);source=engine._connect();journal.install(source);view=MemoryReadView(source)
    try:
        correct(source,fid,'运行预算','并发预算九个')
        saved=tuple(source.execute('SELECT * FROM memory_view_changes ORDER BY seq DESC LIMIT 1').fetchone())
        with source:source.execute('DELETE FROM memory_view_changes WHERE seq=?',(saved[0],))
        with pytest.raises(journal.JournalError):view.recall('运行预算')
        assert view.invalid and not view.ready
        with source:source.execute('INSERT INTO memory_view_changes VALUES (?,?,?,?,?)',saved)
        with pytest.raises(ViewInvalid):view.recall('运行预算')
    finally:view.close();source.close()


def test_constructor_budget_failure_releases_lease_and_leaves_source_open(engine):
    seed(engine);source=engine._connect();journal.install(source)
    with pytest.raises(ViewInvalid,match='snapshot_budget'):MemoryReadView(source,max_bytes=16384)
    assert source.execute('SELECT count(*) FROM memory_view_consumers').fetchone()[0]==0
    assert source.execute('SELECT count(*) FROM working_fact').fetchone()[0]==1
    source.close()


def test_delta_budget_error_rolls_back_view_and_never_returns_old_success(engine):
    fid=seed(engine);source=engine._connect();journal.install(source)
    view=MemoryReadView(source,max_bytes=512*1024)
    try:
        text='x'*(2*1024*1024)
        with source:
            source.execute('UPDATE working_fact SET statement=? WHERE fact_id=?',(text,fid))
            source.execute('UPDATE fact_embeddings SET text_hash=? WHERE fact_id=?',(store._text_hash('运行预算 '+text),fid))
        with pytest.raises(sqlite3.DatabaseError):view.recall('运行预算')
        assert view.invalid and not view.ready
        with pytest.raises(ViewInvalid):view.recall('运行预算')
        assert len(source.execute('SELECT statement FROM working_fact WHERE fact_id=?',(fid,)).fetchone()[0])==len(text)
    finally:view.close();source.close()


def test_default_principal_and_scope_filters_are_not_bypassed(engine):
    seed(engine);foreign=engine.write_fact('预算外部私有信息',subject='运行预算',user_id='foreign')
    engine.flush_background(timeout=30)
    source=engine._connect();journal.install(source);view=MemoryReadView(source)
    try:
        assert all(r['fact_id']!=foreign['fact_id'] for r in view.recall('运行预算')['results'])
        assert not view.recall('运行预算',scopes=['reference'])['results']
        with pytest.raises(ValueError):view.recall('运行预算',_connection=source)
        assert view.ready
    finally:view.close();source.close()


def test_optional_vector_extension_absence_preserves_normal_engine_fallback(engine,monkeypatch):
    from agentcore.memory import vec_index
    monkeypatch.setattr(vec_index,'find_extension',lambda:None)
    monkeypatch.setattr(vec_index,'load_into',lambda conn:False)
    fid=seed(engine);source=engine._connect();journal.install(source);view=MemoryReadView(source)
    try:
        assert view.recall('运行预算')['results'][0]['fact_id']==fid
        query='并发预算'
        assert view.recall(query)==engine.recall_facts(query)
    finally:view.close();source.close()
