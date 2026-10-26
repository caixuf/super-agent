import json
from pathlib import Path
import sqlite3
import time

import pytest

from agentcore.memory import change_journal as journal,memory_store as store
from agentcore.memory.read_view import MemoryReadView,ViewInvalid
from agentcore.memory.recovery_checkpoint import RecoveryCheckpoint
from agentcore.memory.recall_service import ServingMemory,validate_request,private_lock


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
