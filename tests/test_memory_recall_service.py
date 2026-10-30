import json
from pathlib import Path
import sqlite3
import time
import threading

import pytest

from agentcore.memory import change_journal as journal,memory_store as store
from agentcore.memory.read_view import MemoryReadView,ViewInvalid
from agentcore.memory.recovery_checkpoint import RecoveryCheckpoint
from agentcore.memory.recall_service import ServingMemory,validate_request,private_lock,serve,request,RecallUnavailable


def seed(engine):
    result=engine.write_fact('并发预算为七个',subject='服务预算')
    engine.flush_background(timeout=30)
    source=engine._connect();journal.install(source)
    return source,result['fact_id']


def test_checkpoint_is_persisted_before_source_ack_and_failed_save_does_not_advance(engine,tmp_path):
    source,fid=seed(engine);state=RecoveryCheckpoint(tmp_path/'private/state.json',initialize=True)
    state.validate(source);seen=[]
    def checkpoint(consumer,batch):
        seen.append((batch.end_seq,source.execute('SELECT cursor FROM memory_view_consumers WHERE name=?',(consumer.name,)).fetchone()[0]))
        if not batch.baseline:raise OSError('private disk failure body')
        state.save(consumer,batch)
    view=MemoryReadView(source,checkpoint=checkpoint)
    try:
        with source:source.execute("UPDATE working_fact SET status='deprecated' WHERE fact_id=?",(fid,))
        with pytest.raises(OSError):view.recall('服务预算')
        assert not view.ready and view.invalid and seen[-1]==(1,0)
        assert source.execute('SELECT cursor FROM memory_view_consumers').fetchone()[0]==0
        assert state.record['seq']==0
    finally:view.close();source.close()


def test_restart_validates_external_witness_and_rejects_older_source_backup(engine,tmp_path):
    source,fid=seed(engine);path=tmp_path/'private/state.json'
    serving=ServingMemory(path,initialize=True)
    backup=sqlite3.connect(tmp_path/'before.db');source.backup(backup)
    try:
        with source:source.execute("UPDATE working_fact SET status='deprecated' WHERE fact_id=?",(fid,))
        assert not serving.dispatch(dict(op='recall',query='服务预算',identity=serving.identity,
            build_sha256=serving.build,deadline=time.time()+3))['results']
        assert serving.checkpoint.record['seq']>0
        serving.close()
        restarted=ServingMemory(path)
        restarted.close()
        backup.backup(source)
        with pytest.raises((ViewInvalid,journal.JournalError)):ServingMemory(path)
        assert RecoveryCheckpoint(path).record['seq']>0
    finally:backup.close();source.close()


def test_corrupt_checkpoint_cannot_be_reinitialized_over_existing_history(tmp_path):
    p=tmp_path/'private/state.json';p.parent.mkdir(mode=0o700);p.write_text('broken');p.chmod(0o600)
    with pytest.raises(ViewInvalid,match='invalid'):RecoveryCheckpoint(p,initialize=True)
    assert p.read_text()=='broken'


def test_restart_may_advance_witness_when_source_committed_after_last_query(engine,tmp_path):
    source,fid=seed(engine);path=tmp_path/'private/state.json'
    serving=ServingMemory(path,initialize=True)
    before=dict(serving.checkpoint.record);serving.close()
    with source:source.execute("UPDATE working_fact SET status='deprecated' WHERE fact_id=?",(fid,))
    restarted=ServingMemory(path)
    try:
        assert restarted.health()['ready'] and restarted.checkpoint.record['seq']>before['seq']
        for key in ('binding','source_identity','epoch','schema_version'):
            assert restarted.checkpoint.record[key]==before[key]
        assert not restarted.view.recall('服务预算')['results']
    finally:restarted.close();source.close()


def test_idle_maintenance_renews_lease_and_captures_external_retirement(engine,tmp_path):
    source,fid=seed(engine);serving=ServingMemory(tmp_path/'private/state.json',initialize=True,lease_seconds=5)
    try:
        with source:source.execute('UPDATE memory_view_consumers SET lease_until=?',(time.time()+1,))
        serving.maintain()
        assert source.execute('SELECT lease_until FROM memory_view_consumers').fetchone()[0]>time.time()+3
        with source:source.execute("UPDATE working_fact SET status='deprecated' WHERE fact_id=?",(fid,))
        serving.maintain()
        assert serving.health()['ready'] and serving.checkpoint.record['seq']==1
        assert not serving.view.conn.execute("SELECT 1 FROM working_fact WHERE fact_id=? AND status='active'",(fid,)).fetchone()
    finally:serving.close();source.close()


def test_pending_health_exposes_only_known_reason_codes(engine,tmp_path):
    from agentcore.memory.read_view import ViewPending
    source,_=seed(engine); serving=ServingMemory(tmp_path/'private/state.json',initialize=True)
    try:
        serving._state('pending',ViewPending('memory_view_embedding_pending'))
        assert serving.health()['reason_code']=='memory_view_embedding_pending'
        serving._state('pending',ViewPending('sensitive arbitrary text'))
        assert serving.health()['reason_code'] is None
        serving._state('ready')
        assert serving.health()['reason_code'] is None
    finally:serving.close();source.close()


def test_delta_payload_limit_restores_source_limit_and_withdraws_readiness(engine):
    source,fid=seed(engine);view=MemoryReadView(source,max_delta_bytes=4096)
    limit=source.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    try:
        text='x'*8192
        with source:
            source.execute('UPDATE working_fact SET statement=? WHERE fact_id=?',(text,fid))
            source.execute('UPDATE fact_embeddings SET text_hash=? WHERE fact_id=?',(store._text_hash('服务预算 '+text),fid))
        with pytest.raises((ViewInvalid,sqlite3.DataError)):view.recall('服务预算')
        assert view.invalid and not view.ready and view.consumer.seq==0
        assert source.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)==limit
    finally:view.close();source.close()


def test_reconfigured_principal_cannot_keep_serving_old_profile(engine,monkeypatch):
    source,fid=seed(engine);view=MemoryReadView(source)
    try:
        monkeypatch.setattr(store.get_config(),'default_user','another_owner')
        with pytest.raises(ViewInvalid,match='configuration_changed'):view.recall('服务预算')
        assert not view.ready and view.invalid
    finally:view.close();source.close()


@pytest.mark.parametrize('change',[{'identity':{}},{'build_sha256':'stale'},{'kwargs':{'_connection':1}},
    {'kwargs':{'user_id':'foreign'}},{'kwargs':{'limit':True}},{'kwargs':{'scopes':'platform'}},
    {'kwargs':{'ebm':1}},{'kwargs':{'include_unknown':'yes'}},{'deadline':0}])
def test_protocol_rejects_wrong_identity_build_and_untyped_filters(change):
    identity=dict(user_id='owner',agent_id='agent',database_sha256='hash')
    payload=dict(op='recall',query='query',kwargs={},identity=identity,build_sha256='current',deadline=time.time()+3)
    payload.update(change)
    with pytest.raises(ValueError):validate_request(payload,identity,'current')


def test_same_checkpoint_lock_cannot_have_two_writers(tmp_path):
    path=tmp_path/'private/state.lock'
    with private_lock(path):
        with pytest.raises(BlockingIOError):
            with private_lock(path):pytest.fail('second writer must not enter')


def test_health_remains_responsive_and_overload_is_bounded_during_slow_recall(tmp_path):
    entered=threading.Event();release=threading.Event();stop=threading.Event()
    class SlowEngine:
        identity={'user_id':'owner','agent_id':'agent','database_sha256':'hash'}
        build='build'
        def health(self):return {'ready':True,'state':'ready','error_type':None}
        def maintain(self):pass
        def dispatch(self,payload):
            entered.set();release.wait(5)
            return {'results':[]}
    engine=SlowEngine();path=tmp_path/'socket'
    server=threading.Thread(target=serve,args=(engine,path,stop),daemon=True);server.start()
    for _ in range(100):
        if path.exists():break
        time.sleep(.01)
    payload={'op':'recall','query':'test','identity':engine.identity,'build_sha256':engine.build}
    errors=[]
    def run():
        try:request(path,payload,timeout=4)
        except Exception as exc:errors.append(exc)
    first=threading.Thread(target=run);first.start()
    try:
        assert entered.wait(2)
        health=request(path,{'op':'health'},timeout=1)
        assert health['ready'] is False and health['state']=='busy'
        second=threading.Thread(target=run);second.start()
        time.sleep(.1)
        with pytest.raises(RecallUnavailable,match='resident_recall_unavailable') as exc:
            request(path,payload,timeout=1)
        assert exc.value.error_type=='Busy'
        release.set();first.join(3);second.join(3)
        assert not errors
        assert request(path,{'op':'health'},timeout=1)['ready'] is True
    finally:
        release.set();stop.set();server.join(3)
    assert not server.is_alive()


def test_socket_service_uses_real_memory_on_its_owning_thread(engine,tmp_path):
    source,_=seed(engine);stop=threading.Event();started=threading.Event();errors=[];info={}
    path=tmp_path/'socket';checkpoint=tmp_path/'private/state.json'
    def run():
        try:
            memory=ServingMemory(checkpoint,initialize=True)
            info.update(identity=memory.identity,build=memory.build)
            started.set()
            try:serve(memory,path,stop)
            finally:memory.close()
        except Exception as exc:
            errors.append(exc);started.set()
    thread=threading.Thread(target=run);thread.start()
    try:
        assert started.wait(10) and not errors
        for _ in range(100):
            if path.exists():break
            time.sleep(.01)
        result=request(path,{'op':'recall','query':'服务预算',
            'identity':info['identity'],'build_sha256':info['build']},timeout=5)
        assert result['results']
    finally:
        stop.set();thread.join(5);source.close()
    assert not thread.is_alive() and not errors


def test_expired_queued_recall_is_not_executed(tmp_path):
    entered=threading.Event();release=threading.Event();stop=threading.Event();calls=[]
    class SlowEngine:
        identity={'user_id':'owner','agent_id':'agent','database_sha256':'hash'}
        build='build'
        def health(self):return {'ready':True,'state':'ready','error_type':None}
        def maintain(self):pass
        def dispatch(self,payload):
            calls.append(payload['query']);entered.set();release.wait(4);return {'results':[]}
    engine=SlowEngine();path=tmp_path/'socket'
    server=threading.Thread(target=serve,args=(engine,path,stop),daemon=True);server.start()
    for _ in range(100):
        if path.exists():break
        time.sleep(.01)
    def submit(query,timeout):
        try:request(path,{'op':'recall','query':query,'identity':engine.identity,
                          'build_sha256':engine.build},timeout=timeout)
        except (TimeoutError,OSError,RecallUnavailable):pass
    first=threading.Thread(target=submit,args=('first',3));first.start()
    second=None
    try:
        assert entered.wait(2)
        second=threading.Thread(target=submit,args=('expired',.2));second.start();second.join(2)
        assert not second.is_alive()
        release.set();first.join(3)
        time.sleep(.3)
        assert calls==['first']
    finally:
        release.set();stop.set();server.join(3)
        if second is not None:second.join(1)
    assert not server.is_alive()
