import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import threading

import pytest

from agentcore.memory import index_maintenance as maintenance


def fixture_db(tmp_path):
    path = tmp_path/'live-data/live.sqlite'
    path.parent.mkdir()
    with sqlite3.connect(path) as c:
        c.executescript('''CREATE TABLE working_fact(fact_id TEXT PRIMARY KEY,statement TEXT,status TEXT);
            CREATE TABLE fact_embeddings(fact_id TEXT PRIMARY KEY,vector BLOB);
            CREATE TABLE fact_tags(fact_id TEXT,key TEXT,value TEXT,PRIMARY KEY(fact_id,key));
            CREATE TABLE vec_facts(fact_id TEXT PRIMARY KEY,embedding BLOB);''')
        c.execute("INSERT INTO working_fact VALUES('known','preserve exact statement','active')")
        c.executemany('INSERT INTO fact_embeddings VALUES(?,?)',[('known',b'current'),('gone',b'old')])
        c.executemany('INSERT INTO vec_facts VALUES(?,?)',[('known',b'current'),('gone',b'old'),('other',b'orphan')])
        c.executemany('INSERT INTO fact_tags VALUES(?,?,?)',[('known','kind','technical'),('tag-only','kind','old')])
    maintenance.snapshot_plan(path,tmp_path/'backup')
    return path,tmp_path/'backup/plan.json'


def rows(path,table):
    with sqlite3.connect(path) as c:
        return c.execute('SELECT * FROM '+table+' ORDER BY fact_id').fetchall()


def test_cleanup_and_real_recovery_leave_facts_and_valid_indexes_unchanged(tmp_path):
    path,plan = fixture_db(tmp_path)
    facts=rows(path,'working_fact')
    assert maintenance.apply_plan(plan)['deleted_rows']==4
    assert rows(path,'working_fact')==facts
    with maintenance.connection(path) as c:
        assert all(not x['orphan_ids'] for x in maintenance.audit(c)['indexes'].values())
    target=tmp_path/'restored.sqlite';shutil.copy2(path,target)
    assert maintenance.restore_indexes_to_copy(plan,target)['restored_rows']==4
    assert maintenance.restore_indexes_to_copy(plan,target)['restored_rows']==0
    assert rows(target,'working_fact')==facts
    for table in maintenance.TABLES:
        assert rows(target,table)==rows(plan.parent/'memory.sqlite',table)
    assert maintenance.apply_plan(plan)['already_absent']==4


def test_new_writes_reattached_ids_and_changed_orphans_are_not_deleted(tmp_path):
    path,plan = fixture_db(tmp_path)
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO working_fact VALUES('gone','reattached','active')")
        c.execute("UPDATE vec_facts SET embedding=? WHERE fact_id='other'",(b'changed',))
        c.execute("INSERT INTO fact_embeddings VALUES('new-orphan',?)",(b'new',))
    report=maintenance.apply_plan(plan)
    assert report['skipped_reattached']==2 and report['skipped_changed']==1
    assert report['deleted_rows']==1
    assert ('gone',b'old') in rows(path,'vec_facts')
    assert ('other',b'changed') in rows(path,'vec_facts')
    assert ('new-orphan',b'new') in rows(path,'fact_embeddings')


def test_failure_rolls_back_every_index_deletion(tmp_path,monkeypatch):
    path,plan=fixture_db(tmp_path)
    before={t:rows(path,t) for t in maintenance.TABLES}
    original=maintenance._rows;calls=[]
    def fail(conn,table,fid):
        if Path(conn.execute('PRAGMA database_list').fetchone()[2])==path:
            calls.append(fid)
            if len(calls)==2:raise RuntimeError('interrupted maintenance')
        return original(conn,table,fid)
    monkeypatch.setattr(maintenance,'_rows',fail)
    with pytest.raises(RuntimeError,match='interrupted'):
        maintenance.apply_plan(plan)
    assert {t:rows(path,t) for t in maintenance.TABLES}==before


def test_snapshot_tampering_fails_before_source_mutation(tmp_path):
    path,plan=fixture_db(tmp_path)
    with (plan.parent/'memory.sqlite').open('ab') as stream:stream.write(b'tamper')
    with pytest.raises(ValueError,match='hash mismatch'):
        maintenance.apply_plan(plan)
    assert len(rows(path,'vec_facts'))==3


def test_concurrent_writer_waits_for_transaction_and_keeps_its_fact(tmp_path,monkeypatch):
    path,plan=fixture_db(tmp_path)
    enter,started=threading.Event(),threading.Event();errors=[]
    def writer():
        try:
            assert enter.wait(3)
            with sqlite3.connect(path,timeout=5) as c:
                started.set()
                c.execute("INSERT INTO working_fact VALUES('new','concurrent write','active')")
                c.execute("INSERT INTO fact_embeddings VALUES('new',?)",(b'new',))
                c.execute("INSERT INTO vec_facts VALUES('new',?)",(b'new',))
        except BaseException as exc:errors.append(type(exc).__name__)
    original=maintenance._rows
    def pause(conn,table,fid):
        if Path(conn.execute('PRAGMA database_list').fetchone()[2])==path:
            enter.set();assert started.wait(3)
        return original(conn,table,fid)
    monkeypatch.setattr(maintenance,'_rows',pause)
    thread=threading.Thread(target=writer);thread.start()
    assert maintenance.apply_plan(plan)['deleted_rows']==4
    thread.join(5)
    assert not thread.is_alive() and not errors
    assert ('new','concurrent write','active') in rows(path,'working_fact')
    assert ('new',b'new') in rows(path,'vec_facts')


def test_recovery_refuses_live_archive_and_hardlinked_destinations(tmp_path):
    path,plan=fixture_db(tmp_path)
    linked=tmp_path/'linked.sqlite';os.link(path,linked)
    for target in [path,plan.parent/'memory.sqlite',linked]:
        with pytest.raises(ValueError,match='independent copy'):
            maintenance.restore_indexes_to_copy(plan,target)


def test_disaster_recovery_does_not_require_original_database_to_still_exist(tmp_path):
    path,plan=fixture_db(tmp_path)
    maintenance.apply_plan(plan)
    target=tmp_path/'recovery.sqlite';shutil.copy2(path,target);path.unlink()
    assert maintenance.restore_indexes_to_copy(plan,target)['restored_rows']==4


def test_source_and_alias_snapshots_recover_with_the_database(tmp_path):
    path,_=fixture_db(tmp_path)
    archive=path.parent/'curated-snapshots/topic'
    archive.mkdir(parents=True)
    (archive/'revision.md').write_text('【主题】仍成立的原文')
    (archive/'revision.aliases.json').write_text('{"主题":["旧标题"]}')
    maintenance.snapshot_plan(path,tmp_path/'complete-backup',ancillary_roots=[archive.parent])
    plan=tmp_path/'complete-backup/plan.json'
    restored=tmp_path/'isolated-data'
    assert maintenance.restore_ancillary_to_copy(plan,restored)['restored_files']==2
    assert (restored/'curated-snapshots/topic/revision.md').read_text()=='【主题】仍成立的原文'
    assert maintenance.restore_ancillary_to_copy(plan,restored)['restored_files']==0
    with pytest.raises(ValueError,match='isolated destination'):
        maintenance.restore_ancillary_to_copy(plan,path.parent)
    (restored/'curated-snapshots/topic/revision.md').write_text('unrelated new version')
    with pytest.raises(ValueError,match='collision'):
        maintenance.restore_ancillary_to_copy(plan,restored)


def test_corrupted_or_symlinked_ancillary_snapshots_fail_closed(tmp_path):
    path,_=fixture_db(tmp_path)
    archive=path.parent/'curated-snapshots';archive.mkdir()
    resource=archive/'revision.md';resource.write_text('【主题】原文')
    maintenance.snapshot_plan(path,tmp_path/'complete-backup',ancillary_roots=[archive])
    plan=tmp_path/'complete-backup/plan.json'
    (plan.parent/'ancillary/curated-snapshots/revision.md').write_text('corrupt')
    with pytest.raises(ValueError,match='ancillary snapshot mismatch'):
        maintenance.apply_plan(plan)
    resource.unlink();resource.symlink_to('/etc/hostname')
    with pytest.raises(ValueError,match='symlink refused'):
        maintenance.snapshot_plan(path,tmp_path/'unsafe-backup',ancillary_roots=[archive])


def test_bundle_archive_remains_recoverable_after_original_bundle_disappears(tmp_path):
    path,plan=fixture_db(tmp_path)
    destination=tmp_path/'durable-backup'
    result=maintenance.archive_bundle(plan,destination)
    assert result['snapshot_sha256']==maintenance.file_sha(destination/'memory.sqlite')
    maintenance.apply_plan(plan)
    restored=tmp_path/'restored.sqlite';shutil.copy2(path,restored)
    shutil.rmtree(plan.parent)  # Only this test's private temporary fixture.
    assert maintenance.restore_indexes_to_copy(destination/'plan.json',restored)['restored_rows']==4
    with pytest.raises(ValueError,match='independent'):
        maintenance.archive_bundle(destination/'plan.json',destination.parent/'durable-backup')
    occupied=tmp_path/'occupied';occupied.mkdir();(occupied/'keep').write_text('existing backup')
    with pytest.raises(FileExistsError):
        maintenance.archive_bundle(destination/'plan.json',occupied)
    assert (occupied/'keep').read_text()=='existing backup'
