"""Candidate filtering must stay bounded even when nearly all facts share an owner."""
import sqlite3

from agentcore.memory import memory_store as store,vec_index


def test_skewed_owner_index_does_not_turn_two_candidates_into_an_owner_scan(monkeypatch):
    conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
    conn.executescript('''
        CREATE TABLE working_fact(fact_id TEXT PRIMARY KEY,user_id TEXT,status TEXT,workspace_id TEXT);
        CREATE INDEX idx_fact_user_status ON working_fact(user_id,status);
    ''')
    conn.executemany('INSERT INTO working_fact VALUES (?,?,?,?)',
        [(f'f{i:05d}','owner','active','_unknown') for i in range(10000)]+
        [('foreign','another-owner','active','_unknown'),('retired','owner','deprecated','_unknown')])
    monkeypatch.setattr(vec_index,'find_extension',lambda:True)
    monkeypatch.setattr(store,'_embed_with_cache',lambda _: [[1.0,0.0]])
    monkeypatch.setattr(vec_index,'knn',lambda *a,**kw:
        [('f00001',.1),('f00005',.2),('foreign',.3),('retired',.4),('f00001',.1)])
    traced=[];conn.set_trace_callback(traced.append)
    rowids=store._knn_rowids(conn,'query',user_id='owner')
    assert len(rowids)==2
    names={r[0] for r in conn.execute('SELECT fact_id FROM working_fact WHERE rowid IN (?,?)',rowids)}
    assert names=={'f00001','f00005'}
    statement=next(sql for sql in traced if sql.startswith('WITH candidate_ids'))
    plan=[row[3] for row in conn.execute('EXPLAIN QUERY PLAN '+statement)]
    assert any('sqlite_autoindex_working_fact_1' in row for row in plan)
    assert not any('idx_fact_user_status' in row for row in plan)
    conn.close()


def test_batch_tags_preserves_missing_and_selected_rows_with_bounded_parameters():
    conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
    conn.execute('CREATE TABLE fact_tags(fact_id TEXT,key TEXT,value TEXT,PRIMARY KEY(fact_id,key))')
    conn.executemany('INSERT INTO fact_tags VALUES (?,?,?)',
        [(f'f{i}','kind','personal' if i%2 else 'technical') for i in range(1001)]+
        [('foreign','private','do-not-return')])
    traced=[];conn.set_trace_callback(traced.append)
    ids=[f'f{i}' for i in range(1001)]+['missing','f0']
    actual=store._tags_of_many(conn,ids)
    assert actual['f0']=={'kind':'technical'} and actual['f1']=={'kind':'personal'}
    assert actual['missing']=={} and 'foreign' not in actual and len(actual)==1002
    assert len([sql for sql in traced if sql.startswith('SELECT fact_id,key,value')])==3
    assert store._tags_of_many(conn,[])=={}
    conn.close()
