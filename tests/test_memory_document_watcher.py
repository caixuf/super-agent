import pytest

from agentcore.memory.document_watcher import DocumentWatcher
from agentcore.memory.curated_sync import sync_document
from agentcore.memory import memory_store as store


def test_live_refresh_retires_old_revision_without_losing_independent_facts(tmp_path,configured):
    path=tmp_path/'MEMORY.md';path.write_text('【预算】并发上限七个。')
    tick=[0];calls=[]
    def sync(source,**kw):
        calls.append(kw['expected_revision'])
        return sync_document(source,document_id='personal/memory',**kw)
    with configured(tmp_path/'data'):
        watcher=DocumentWatcher(path,sync,clock=lambda:tick[0])
        assert watcher.poll()['state']=='settling'
        tick[0]=5
        assert watcher.poll()['state']=='published'
        first=store.recall_facts('预算')['results'][0]['fact_id']
        independent=store.write_fact('独立用户事实仍需保留',subject='独立事实')
        for _ in range(3):assert watcher.poll()['state']=='unchanged'
        assert len(calls)==1
        path.write_text('【预算】并发上限九个。')
        assert watcher.poll()['state']=='settling'
        tick[0]=10
        assert watcher.poll()['state']=='published'
        assert '九个' in store.recall_facts('预算')['results'][0]['statement']
        conn=store._connect()
        try:
            assert conn.execute('SELECT status FROM working_fact WHERE fact_id=?',(first,)).fetchone()[0]=='deprecated'
            assert conn.execute('SELECT status FROM working_fact WHERE fact_id=?',(independent['fact_id'],)).fetchone()[0]=='active'
        finally:conn.close()


def test_retry_is_bounded_and_changed_source_supersedes_failed_work(tmp_path):
    path=tmp_path/'MEMORY.md';path.write_text('original');tick=[0];calls=[]
    def broken(source,**kw):
        calls.append(kw);raise RuntimeError('secret private source contents')
    w=DocumentWatcher(path,broken,clock=lambda:tick[0])
    assert w.poll()['state']=='settling';tick[0]=5
    result=w.poll();assert result['state']=='retry' and 'secret' not in str(result)
    tick[0]=10;assert w.poll()['state']=='waiting' and len(calls)==1
    path.write_text('new');assert w.poll()['state']=='settling'
    tick[0]=15;assert w.poll()['state']=='retry' and len(calls)==2
    path.unlink();assert w.poll()['state']=='source_unavailable'
    assert w.published is None


@pytest.mark.parametrize('outcome',[None,{'ok':False},{'ok':True,'revision':'wrong'}])
def test_incomplete_or_wrong_revision_does_not_become_success(tmp_path,outcome):
    path=tmp_path/'MEMORY.md';path.write_text('one');w=DocumentWatcher(path,lambda *a,**k:outcome,settle_seconds=0)
    w.poll();assert w.poll()['state']=='retry' and w.published is None


def test_change_during_sync_is_observed_again_and_never_marked_current(tmp_path):
    path=tmp_path/'MEMORY.md';path.write_text('one')
    def racing(source,**kw):
        source.write_text('two');return {'ok':True,'revision':kw['expected_revision']}
    w=DocumentWatcher(path,racing,settle_seconds=0)
    w.poll();assert w.poll()['state']=='source_changed' and w.published is None
    assert w.poll()['state']=='settling'


def test_oversized_source_never_reaches_sync(tmp_path):
    path=tmp_path/'MEMORY.md';path.write_bytes(b'x'*(512*1024+1))
    def forbidden(*a,**k):raise AssertionError('not called')
    assert DocumentWatcher(path,forbidden).poll()['state']=='source_unavailable'


def test_candidate_identity_replay_uses_primary_key_and_rejects_collisions(tmp_path,configured,monkeypatch):
    with configured(tmp_path/'data'):
        original=store.write_fact('相同源内容',subject='标题',fact_id='stable',source='curated_profile:test',initial_status='candidate')
        assert original['written']
        store.flush_background(timeout=30)
        connect=store._connect;statements=[]
        def traced():
            conn=connect();conn.set_trace_callback(statements.append);return conn
        monkeypatch.setattr(store,'_connect',traced)
        again=store.write_fact('相同源内容',subject='标题',fact_id='stable',source='curated_profile:test',initial_status='candidate')
        assert again['ok'] and not again['written']
        assert not any('WHERE user_id=' in sql for sql in statements)
        with pytest.raises(ValueError,match='identity_conflict'):
            store.write_fact('相同源内容',subject='标题',fact_id='stable',source='another-source',initial_status='candidate')
