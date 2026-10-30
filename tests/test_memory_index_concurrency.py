import sqlite3

import pytest

from agentcore.memory import vec_index


def test_rebuild_does_not_overwrite_newer_vector_index(tmp_path, monkeypatch):
    path = tmp_path / "memory.db"
    conn = sqlite3.connect(path)
    other = sqlite3.connect(path)
    try:
        conn.executescript("""
            CREATE TABLE working_fact(fact_id TEXT PRIMARY KEY, user_id TEXT, status TEXT,
                                      subject TEXT, statement TEXT);
            CREATE TABLE fact_embeddings(fact_id TEXT PRIMARY KEY, model TEXT, vector BLOB);
            INSERT INTO working_fact VALUES('f','u','active','主题','旧内容');
        """)
        if not vec_index.ensure_table(conn, 2):
            pytest.skip("sqlite-vec extension unavailable")
        assert vec_index.load_into(other)
        old, new = [1.0, 0.0], [0.0, 1.0]
        conn.execute("INSERT INTO fact_embeddings VALUES(?,?,?)", ('f', 'model', vec_index.serialize(old)))
        conn.commit()
        original_ensure = vec_index.ensure_table
        def update_before_index(c, dim):
            ok = original_ensure(c, dim)
            with other:
                other.execute("UPDATE working_fact SET statement='新内容' WHERE fact_id='f'")
                other.execute("UPDATE fact_embeddings SET vector=? WHERE fact_id='f'", (vec_index.serialize(new),))
                other.execute("INSERT INTO vec_facts(fact_id,embedding) VALUES(?,?)", ('f', vec_index.serialize(new)))
            return ok
        monkeypatch.setattr(vec_index, 'ensure_table', update_before_index)
        result = vec_index.upsert_current(conn, [('f', old, '主题', '旧内容')],
                                          user_id='u', model='model', dim=2)
        assert result == {'available': True, 'indexed': 0, 'skipped_changed': 1, 'failed': 0}
        stored = conn.execute("SELECT embedding FROM vec_facts WHERE fact_id='f'").fetchone()[0]
        assert stored == vec_index.serialize(new)
    finally:
        other.close()
        conn.close()


def test_rebuild_indexes_unchanged_current_vector(tmp_path):
    conn = sqlite3.connect(tmp_path / 'memory.db')
    try:
        conn.executescript("""
            CREATE TABLE working_fact(fact_id TEXT PRIMARY KEY, user_id TEXT, status TEXT,
                                      subject TEXT, statement TEXT);
            CREATE TABLE fact_embeddings(fact_id TEXT PRIMARY KEY, model TEXT, vector BLOB);
            INSERT INTO working_fact VALUES('f','u','active','主题','内容');
        """)
        if not vec_index.ensure_table(conn, 2):
            pytest.skip("sqlite-vec extension unavailable")
        vec = [1.0, 0.0]
        conn.execute("INSERT INTO fact_embeddings VALUES(?,?,?)", ('f', 'model', vec_index.serialize(vec)))
        conn.commit()
        result = vec_index.upsert_current(conn, [('f', vec, '主题', '内容')],
                                          user_id='u', model='model', dim=2)
        assert result == {'available': True, 'indexed': 1, 'skipped_changed': 0, 'failed': 0}
        assert conn.execute("SELECT embedding FROM vec_facts WHERE fact_id='f'").fetchone()[0] == vec_index.serialize(vec)
    finally:
        conn.close()
