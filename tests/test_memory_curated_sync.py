from pathlib import Path

import pytest

from agentcore.memory import memory_store as store
from agentcore.memory.curated_sync import sync_document


def document(path, value='并发预算固定为七个'):
    path.write_text('【演练预算】'+value+'。\n§\n【报告格式】先给结论再给证据。\n')
    return path


def test_publication_is_idempotent_and_original_snapshot_is_recoverable(tmp_path, configured):
    path = document(tmp_path/'MEMORY.md')
    with configured(tmp_path/'data'):
        first = sync_document(path, document_id='owner/memory')
        assert first['ok']
        assert Path(first['snapshot']).read_bytes() == path.read_bytes()
        again = sync_document(path, document_id='owner/memory')
        assert again['fact_ids'] == first['fact_ids'] and again['retired'] == 0
        result = store.recall_facts('演练预算')
        assert result['results'][0]['rank_mode'] == 'exact_subject'
        assert '七个' in result['results'][0]['statement']


def test_revision_retires_old_sections_without_deleting_them(tmp_path, configured):
    path = document(tmp_path/'MEMORY.md')
    with configured(tmp_path/'data'):
        first = sync_document(path, document_id='owner/memory')
        document(path, '并发预算固定为九个')
        second = sync_document(path, document_id='owner/memory')
        assert second['ok'] and second['retired'] == 1
        assert '九个' in store.recall_facts('演练预算')['results'][0]['statement']
        conn = store._connect()
        try:
            statuses = {r['status'] for r in conn.execute('SELECT status FROM working_fact')}
            assert statuses == {'active', 'deprecated'}
        finally:
            conn.close()
        assert '七个' in Path(first['snapshot']).read_text()


def test_reviewed_alias_survives_heading_merge_and_is_source_owned(tmp_path, configured):
    path = tmp_path/'MEMORY.md'
    path.write_text('【运行与调度】并发预算固定为七个。\n')
    with configured(tmp_path/'data'):
        first = sync_document(path, document_id='owner/memory',
                              aliases={'运行与调度': ['演练预算']})
        result = store.recall_facts('演练预算')['results']
        assert len(result) == 1 and result[0]['subject'] == '运行与调度'
        assert result[0]['tags']['subject_alias_0'] == '演练预算'
        assert store.recall_facts('演练预算是多少')['results'][0]['fact_id'] == result[0]['fact_id']
        assert not store.recall_facts('演练预算', user_id='someone-else')['results']
        assert Path(first['alias_snapshot']).read_text() == '{"运行与调度": ["演练预算"]}'
        sync_document(path, document_id='owner/memory', aliases={})
        conn = store._connect()
        try:
            assert not conn.execute("SELECT 1 FROM fact_tags WHERE key='subject_alias_0'").fetchall()
        finally:
            conn.close()


def test_aliases_cannot_refer_to_a_missing_heading(tmp_path, configured):
    path = document(tmp_path/'MEMORY.md')
    with configured(tmp_path/'data'), pytest.raises(ValueError, match='alias_subject'):
        sync_document(path, document_id='owner/memory', aliases={'不存在的标题': ['旧标题']})


def test_archived_revision_and_alias_restore_preserve_new_user_facts(tmp_path,configured):
    from agentcore.memory.curated_sync import document_manifest,restore_document_revision
    path=document(tmp_path/'MEMORY.md')
    with configured(tmp_path/'data'):
        first=sync_document(path,document_id='owner/memory',aliases={'演练预算':['旧预算']})
        document(path,'并发预算固定为九个')
        second=sync_document(path,document_id='owner/memory',aliases={'演练预算':['新预算']})
        newcomer=store.write_fact('新会话决定保留独立报告',subject='新决定',source='user_turn')
        assert document_manifest('owner/memory')['alias_revision']==second['alias_revision']
        assert restore_document_revision(document_id='owner/memory',revision=first['revision'],
                                         alias_revision=first['alias_revision'])['ok']
        assert '七个' in store.recall_facts('旧预算')['results'][0]['statement']
        assert document_manifest('owner/memory')['fact_ids']==first['fact_ids']
        assert restore_document_revision(document_id='owner/memory',revision=second['revision'],
                                         alias_revision=second['alias_revision'])['ok']
        assert '九个' in store.recall_facts('新预算')['results'][0]['statement']
        conn=store._connect()
        try:assert conn.execute('SELECT status FROM working_fact WHERE fact_id=?',(newcomer['fact_id'],)).fetchone()[0]=='active'
        finally:conn.close()
        assert '九个' in path.read_text()  # Restoration never rewrites the prompt.


def test_wrong_or_tampered_revision_cannot_publish(tmp_path,configured):
    from agentcore.memory.curated_sync import document_manifest,restore_document_revision
    path=document(tmp_path/'MEMORY.md')
    with configured(tmp_path/'data'):
        first=sync_document(path,document_id='owner/memory')
        Path(first['snapshot']).write_text('【演练预算】被篡改的内容')
        before=document_manifest('owner/memory')
        with pytest.raises(ValueError,match='revision_mismatch'):
            restore_document_revision(document_id='owner/memory',revision=first['revision'])
        assert document_manifest('owner/memory')==before
        with pytest.raises(ValueError,match='invalid_curated_revision'):
            restore_document_revision(document_id='owner/memory',revision='../outside')


def test_long_named_phrase_finds_curated_context_without_matching_a_book(tmp_path, configured):
    path = tmp_path/'MEMORY.md'
    path.write_text('【运行岗位】记忆预算守护每六小时检查一次。\n')
    with configured(tmp_path/'data'):
        assert sync_document(path, document_id='owner/memory')['ok']
        store.write_fact('记忆预算守护在某教材例子中出现', subject='书籍示例', source='book_ingest')
        recalled = store.recall_facts('记忆预算守护 压缩')
        assert len(recalled['results']) == 1
        assert recalled['results'][0]['rank_mode'] == 'curated_phrase'
        assert recalled['results'][0]['subject'] == '运行岗位'
        assert store.recall_facts('记忆预算守护 压缩', user_id='someone-else')['results'] == []


def test_failed_index_cannot_publish_candidates_over_previous_revision(tmp_path, configured, monkeypatch):
    path = document(tmp_path/'MEMORY.md')
    with configured(tmp_path/'data'):
        first = sync_document(path, document_id='owner/memory')
        document(path, '并发预算固定为九个')
        original_flush = store.flush_background
        monkeypatch.setattr(store, 'flush_background', lambda **kw: 1)
        result = sync_document(path, document_id='owner/memory')
        assert result['reason'] == 'curated_index_pending'
        assert '七个' in store.recall_facts('演练预算')['results'][0]['statement']
        monkeypatch.setattr(store, 'flush_background', original_flush)
        assert sync_document(path, document_id='owner/memory')['ok']
        assert '九个' in store.recall_facts('演练预算')['results'][0]['statement']


def test_source_changed_while_indexing_stays_unpublished(tmp_path, configured, monkeypatch):
    path = document(tmp_path/'MEMORY.md')
    with configured(tmp_path/'data'):
        original_flush = store.flush_background
        def changed(**kwargs):
            result = original_flush(**kwargs)
            document(path, '并发预算固定为十一个')
            return result
        monkeypatch.setattr(store, 'flush_background', changed)
        result = sync_document(path, document_id='owner/memory')
        assert result['reason'] == 'curated_source_changed'
        assert store.recall_facts('演练预算')['results'] == []


def test_other_writer_and_user_are_not_retired_by_document_publication(tmp_path, configured):
    path = document(tmp_path/'MEMORY.md')
    with configured(tmp_path/'data'):
        user_fact = store.write_fact('用户决定保留独立的回执', subject='独立决定', source='auto_trigger')
        other_fact = store.write_fact('另一用户的演练预算', subject='演练预算', user_id='other')
        assert sync_document(path, document_id='owner/memory')['ok']
        document(path, '并发预算固定为九个')
        assert sync_document(path, document_id='owner/memory')['ok']
        conn = store._connect()
        try:
            for fid in [user_fact['fact_id'], other_fact['fact_id']]:
                assert conn.execute('SELECT status FROM working_fact WHERE fact_id=?', (fid,)).fetchone()['status'] == 'active'
        finally:
            conn.close()
        assert len(store.recall_facts('演练预算', user_id='other')['results']) == 1


def test_identifier_pointer_is_a_whole_word_and_ambiguous_phrases_fall_back(tmp_path, configured, monkeypatch):
    path = tmp_path/'MEMORY.md'
    path.write_text('【Pi 执行器】按回执判断运行成功，commit 只用于版本登记。\n§\n'
                    '【MPI 通信】这是另一套协议，commit 只用于版本登记。\n')
    with configured(tmp_path/'data'):
        assert sync_document(path, document_id='owner/memory')['ok']
        result = store.recall_facts('Pi 完成后怎么看回执')
        assert len(result['results']) == 1
        assert result['results'][0]['subject'] == 'Pi 执行器'
        assert result['results'][0]['rank_mode'] == 'curated_identifier'
        ranked = []
        monkeypatch.setattr(store, '_rank', lambda *a, **kw: ranked.append(True) or [])
        result = store.recall_facts('commit 规则')
        assert result['results'] == []
        assert ranked, 'ambiguous common words must go through ranking'


def test_named_topic_does_not_walk_every_fact(tmp_path, configured, monkeypatch):
    path = document(tmp_path/'MEMORY.md')
    with configured(tmp_path/'data'):
        assert sync_document(path, document_id='owner/memory')['ok']
        conn = store._connect()
        try:
            with conn:
                conn.executemany('INSERT INTO working_fact(fact_id,user_id,statement,status,created_at) VALUES(?,?,?,?,?)',
                    [(str(i), 'default_user', 'unrelated background material', 'active', '2026-01-01') for i in range(10000)])
        finally:
            conn.close()
        original = store._connect
        def bounded_connection():
            conn = original()
            calls = [0]
            def budget():
                calls[0] += 1
                return int(calls[0] > 5)
            conn.set_progress_handler(budget, 1000)
            return conn
        monkeypatch.setattr(store, '_connect', bounded_connection)
        result = store.recall_facts('演练预算')
        assert result['results'][0]['rank_mode'] == 'exact_subject'


@pytest.mark.parametrize('text', ['', 'unstructured text', '【同名】a\n§\n【同名】b'])
def test_invalid_document_does_not_publish(tmp_path, configured, text):
    path = tmp_path/'MEMORY.md'
    path.write_text(text)
    with configured(tmp_path/'data'):
        with pytest.raises(ValueError):
            sync_document(path, document_id='owner/memory')
