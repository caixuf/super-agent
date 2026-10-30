from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from agentcore.memory import change_journal as journal


@pytest.fixture
def db(engine):
    conn=engine._connect()
    try:yield conn
    finally:conn.close()


def fact(conn,name):
    conn.execute('INSERT INTO working_fact(fact_id,user_id,statement,created_at) VALUES (?,?,?,?)',
                 (name,'owner','isolated fact','2026-01-01'))


def embedding(conn,name):
    conn.execute('INSERT INTO fact_embeddings VALUES (?,?,?,?,?,?)',
                 (name,'model',1,b'1234','hash','2026-01-01'))


def test_all_nine_source_operations_are_captured_with_their_transaction(db):
    journal.install(db);consumer=journal.register(db,'reader')
    fact(db,'one');db.execute("INSERT INTO fact_tags VALUES ('one','scope','platform')");embedding(db,'one')
    db.execute("UPDATE working_fact SET status='deprecated' WHERE fact_id='one'")
    db.execute("UPDATE fact_tags SET value='reference' WHERE fact_id='one'")
    db.execute("UPDATE fact_embeddings SET vector=x'01020304' WHERE fact_id='one'")
    for table in ('fact_tags','fact_embeddings','working_fact'):db.execute('DELETE FROM '+table)
    db.commit()
    with journal.read_changes(db,consumer) as batch:
        assert batch.caught_up and len(batch.events)==9
        assert {(row[3],row[4]) for row in batch.events}=={
            (entity,op) for entity in journal.TABLES for op in ('insert','update','delete')}
        assert not db.execute('SELECT * FROM working_fact').fetchall()
    consumer=journal.acknowledge(db,consumer,batch)
    assert journal.prune(db,retain_events=0)['floor']==9
    with journal.read_changes(db,consumer) as empty:assert not empty.events and empty.caught_up


def test_source_rollback_cannot_leave_a_false_committed_change(db):
    journal.install(db);consumer=journal.register(db,'reader')
    fact(db,'one');db.rollback()
    with journal.read_changes(db,consumer) as batch:assert not batch.events
    assert db.execute('SELECT count(*) FROM working_fact').fetchone()[0]==0
    fact(db,'two');db.commit()
    with journal.read_changes(db,consumer) as batch:assert batch.end_seq==1


def test_baseline_snapshot_and_concurrent_write_have_no_handoff_gap(db,engine):
    fact(db,'before');db.commit();journal.install(db);consumer=journal.register(db,'reader')
    writer=engine._connect()
    try:
        with journal.read_baseline(db,consumer) as baseline:
            assert baseline.baseline and baseline.end_seq==0
            assert [r[0] for r in db.execute('SELECT fact_id FROM working_fact')]==['before']
            fact(writer,'during');writer.execute("INSERT INTO fact_tags VALUES ('during','scope','platform')")
            embedding(writer,'during');writer.commit()
            assert [r[0] for r in db.execute('SELECT fact_id FROM working_fact')]==['before']
        consumer=journal.acknowledge(db,consumer,baseline)
        with journal.read_changes(db,consumer) as delta:
            assert len(delta.events)==3 and delta.caught_up
            assert {r[2] for r in delta.events}=={'during'}
            assert db.execute("SELECT count(*) FROM working_fact WHERE fact_id='during'").fetchone()[0]==1
    finally:writer.close()


def test_primary_key_rewrite_records_old_and_new_identity(db):
    fact(db,'old');db.commit();journal.install(db);consumer=journal.register(db,'reader')
    db.execute("UPDATE working_fact SET fact_id='new' WHERE fact_id='old'");db.commit()
    with journal.read_changes(db,consumer) as batch:
        assert [(r[2],r[4]) for r in batch.events]==[('old','update_old'),('new','update')]


def test_missing_middle_event_is_never_reported_as_caught_up(db):
    journal.install(db);consumer=journal.register(db,'reader')
    for i in range(3):fact(db,str(i))
    db.commit();db.execute('DELETE FROM memory_view_changes WHERE seq=2');db.commit()
    with journal.read_changes(db,consumer,limit=1) as batch:assert not batch.caught_up
    consumer=journal.acknowledge(db,consumer,batch)
    with pytest.raises(journal.JournalError,match='change_gap'):
        with journal.read_changes(db,consumer,limit=1):pytest.fail('must not yield a batch')


def test_consumed_history_branch_is_detected_even_with_same_epoch_and_sequence(db):
    journal.install(db);consumer=journal.register(db,'reader');fact(db,'one');db.commit()
    with journal.read_changes(db,consumer) as batch:pass
    consumer=journal.acknowledge(db,consumer,batch)
    fact(db,'two');db.commit()
    db.execute("UPDATE memory_view_changes SET token=? WHERE seq=1",('f'*32,));db.commit()
    with pytest.raises(journal.JournalError,match='history_changed'):
        with journal.read_changes(db,consumer):pytest.fail('must not yield')


def test_same_path_database_replacement_invalidates_old_connection(db,tmp_path):
    journal.install(db);consumer=journal.register(db,'reader')
    path=Path(next(r[2] for r in db.execute('PRAGMA database_list') if r[1]=='main'))
    replacement=tmp_path/'replacement.db';other=sqlite3.connect(replacement)
    try:db.backup(other)
    finally:other.close()
    replacement.replace(path)
    with pytest.raises(journal.JournalError,match='source_replaced'):
        with journal.read_changes(db,consumer):pytest.fail('must not yield')


def test_capture_trigger_drop_and_recreate_invalidates_existing_view(db):
    journal.install(db);consumer=journal.register(db,'reader')
    name=next(iter(journal.TRIGGERS));db.execute('DROP TRIGGER '+name);db.execute(journal.TRIGGERS[name]);db.commit()
    with pytest.raises(journal.JournalError,match='schema_changed'):
        with journal.read_changes(db,consumer):pytest.fail('must not yield')


def test_new_source_unique_constraint_requires_capture_migration(db):
    journal.install(db);consumer=journal.register(db,'reader')
    db.execute('CREATE UNIQUE INDEX new_source_constraint ON working_fact(user_id,statement)');db.commit()
    with pytest.raises(journal.JournalError,match='source_schema'):
        with journal.read_changes(db,consumer):pytest.fail('must not yield')


def test_lease_expiry_rebuild_and_old_owner_fencing(db):
    journal.install(db);old=journal.register(db,'reader',lease_seconds=5,now=100)
    journal.renew(db,old,now=104)
    with journal.read_changes(db,old,now=108):pass
    with pytest.raises(journal.JournalError,match='lease_lost'):journal.renew(db,old,now=109)
    new=journal.register(db,'reader',lease_seconds=5,now=110)
    assert new.owner!=old.owner
    with pytest.raises(journal.JournalError,match='lease_lost'):
        with journal.read_changes(db,old,now=111):pytest.fail('old owner must not resume')
    with journal.read_baseline(db,new,now=111) as batch:assert batch.baseline


def test_live_consumers_pin_history_and_full_log_rejects_source_mutation(db):
    journal.install(db,max_events=4);slow=journal.register(db,'slow');fast=journal.register(db,'fast')
    for i in range(4):fact(db,str(i))
    db.commit()
    with pytest.raises(sqlite3.IntegrityError,match='change_capacity'):fact(db,'overflow')
    db.rollback()
    assert not db.execute("SELECT 1 FROM working_fact WHERE fact_id='overflow'").fetchone()
    with journal.read_changes(db,fast) as batch:pass
    fast=journal.acknowledge(db,fast,batch)
    assert journal.prune(db,retain_events=0)['floor']==0
    journal.release(db,slow)
    assert journal.prune(db)['remaining']<4
    fact(db,'after-prune');db.commit()
    with journal.read_changes(db,fast) as batch:assert batch.end_seq==5


def test_consumer_budget_and_expired_reader_do_not_pin_log_forever(db):
    journal.install(db,max_events=4,max_consumers=1)
    old=journal.register(db,'one',lease_seconds=1,now=100)
    with pytest.raises(journal.JournalError,match='consumer_capacity'):journal.register(db,'two',now=100)
    fact(db,'one');db.commit()
    assert journal.prune(db,retain_events=0,now=102)['floor']==1
    new=journal.register(db,'two',now=102)
    with journal.read_baseline(db,new,now=102) as batch:assert batch.end_seq==1


def test_capacity_error_halfway_through_pk_rewrite_rolls_back_both_log_and_row(db):
    fact(db,'old');db.commit();journal.install(db,max_events=4)
    consumer=journal.register(db,'reader')
    for i in range(3):fact(db,'filler'+str(i))
    db.commit()
    with pytest.raises(sqlite3.IntegrityError,match='change_capacity'):
        db.execute("UPDATE working_fact SET fact_id='new' WHERE fact_id='old'")
    db.rollback()
    assert db.execute("SELECT 1 FROM working_fact WHERE fact_id='old'").fetchone()
    assert not db.execute("SELECT 1 FROM working_fact WHERE fact_id='new'").fetchone()
    with journal.read_changes(db,consumer) as batch:
        assert batch.end_seq==3 and all(r[2].startswith('filler') for r in batch.events)


def test_bulk_update_capacity_error_does_not_commit_partial_status_changes(db):
    for i in range(5):fact(db,str(i))
    db.commit();journal.install(db,max_events=4);consumer=journal.register(db,'reader')
    with pytest.raises(sqlite3.IntegrityError,match='change_capacity'):
        db.execute("UPDATE working_fact SET status='deprecated'")
    db.rollback()
    assert {r[0] for r in db.execute('SELECT status FROM working_fact')}=={'active'}
    with journal.read_changes(db,consumer) as batch:assert not batch.events


def test_replace_with_recursive_triggers_disabled_still_captures_same_identity(db):
    fact(db,'one');db.execute("INSERT INTO fact_tags VALUES ('one','scope','platform')");embedding(db,'one');db.commit()
    db.execute('PRAGMA recursive_triggers=OFF');journal.install(db);consumer=journal.register(db,'reader')
    db.execute("INSERT OR REPLACE INTO working_fact(fact_id,user_id,statement,created_at) VALUES ('one','owner','replaced','2026')")
    db.execute("INSERT OR REPLACE INTO fact_tags VALUES ('one','scope','reference')")
    db.execute("INSERT OR REPLACE INTO fact_embeddings VALUES ('one','model',1,x'01020304','new','2026')");db.commit()
    with journal.read_changes(db,consumer) as batch:
        assert len(batch.events)==3 and {r[3] for r in batch.events}==set(journal.TABLES)
        assert {r[2] for r in batch.events}=={'one'}
        assert db.execute("SELECT statement FROM working_fact WHERE fact_id='one'").fetchone()[0]=='replaced'


def test_failed_ack_and_stale_ack_cannot_advance_consumer(db):
    journal.install(db);consumer=journal.register(db,'reader');fact(db,'one');db.commit()
    with journal.read_changes(db,consumer) as batch:pass
    with pytest.raises(journal.JournalError,match='invalid_ack'):
        journal.acknowledge(db,consumer,replace(batch,end_seq=2))
    updated=journal.acknowledge(db,consumer,batch)
    with pytest.raises(journal.JournalError,match='cursor_changed'):journal.acknowledge(db,consumer,batch)
    with journal.read_changes(db,updated) as empty:assert empty.caught_up


def test_idempotent_install_does_not_reset_generation_or_policy(db):
    epoch=journal.install(db,max_events=8)
    assert journal.install(db,max_events=8)==epoch
    with pytest.raises(journal.JournalError,match='budget_change'):journal.install(db,max_events=16)
    name=next(iter(journal.TRIGGERS));db.execute('DROP TRIGGER '+name);db.commit()
    with pytest.raises(journal.JournalError,match='capture_schema'):journal.install(db,max_events=8)


@pytest.mark.parametrize('identity',[None,'','中'*1000])
def test_unbounded_or_null_identity_cannot_bypass_capture(db,identity):
    journal.install(db);consumer=journal.register(db,'reader')
    with pytest.raises(sqlite3.IntegrityError,match='invalid_identity'):fact(db,identity)
    db.rollback()
    with journal.read_changes(db,consumer) as batch:assert not batch.events
