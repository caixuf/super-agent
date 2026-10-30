"""Rehearse embedding archive recovery in a newly created SQLite copy only."""
import base64
import json
import os
from pathlib import Path
import sqlite3
import stat

from . import change_journal, memory_store, vec_index


def restore_to_new_copy(*, archive_path, source_path, destination_path):
    archive = Path(archive_path)
    info = archive.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
        raise ValueError('archive must be a private regular file')
    with archive.open('rb') as stream:
        data = stream.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise ValueError('archive too large')
    record = json.loads(data)
    if (not isinstance(record, dict) or record.get('version') != 1
            or type(record.get('index_exists')) is not bool
            or not isinstance(record.get('entries'), list)
            or not 1 <= len(record['entries']) <= 32):
        raise ValueError('invalid archive')
    source = Path(source_path).resolve(strict=True)
    identity = source.stat()
    if record.get('source_identity') != [str(source), identity.st_dev, identity.st_ino]:
        raise ValueError('source does not match archive')
    # O_EXCL ensures neither the source, a symlink, nor any existing file can
    # be selected as the output. Only the newly created copy is ever modified.
    target = Path(destination_path)
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    os.close(fd)
    source_conn = target_conn = None
    try:
        source_conn = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=10)
        if list(change_journal._identity(source_conn)) != record['source_identity']:
            raise ValueError('source changed while opening')
        target_conn = sqlite3.connect(target, timeout=10)
        source_conn.backup(target_conn, pages=256, sleep=0.05)
        if record['index_exists'] and not vec_index.load_into(target_conn):
            raise ValueError('vector extension required for archive recovery')
        meta = change_journal._validate(target_conn)
        if meta['epoch'] != record.get('journal_epoch'):
            raise ValueError('journal epoch does not match archive')
        target_conn.execute('BEGIN IMMEDIATE')
        ids = set()
        for item in record['entries']:
            fid = item['fact_id']
            if not isinstance(fid, str) or not fid or fid in ids:
                raise ValueError('invalid archive identity')
            ids.add(fid)
            fact = target_conn.execute('SELECT subject,statement FROM working_fact WHERE fact_id=?', (fid,)).fetchone()
            if not fact or memory_store._text_hash(((fact[0] or '') + ' ' + (fact[1] or '')).strip()) != item['fact_text_hash']:
                raise ValueError('fact changed since archive')
            old = item['embedding']
            if old is None:
                target_conn.execute('DELETE FROM fact_embeddings WHERE fact_id=?', (fid,))
            else:
                vector = base64.b64decode(old['vector_b64'], validate=True)
                if type(old['dim']) is not int or not 1 <= old['dim'] <= 4096 or len(vector) != old['dim'] * 4:
                    raise ValueError('invalid archived vector')
                target_conn.execute(
                    'INSERT INTO fact_embeddings(fact_id,model,dim,vector,text_hash,updated_at) VALUES (?,?,?,?,?,?) '
                    'ON CONFLICT(fact_id) DO UPDATE SET model=excluded.model,dim=excluded.dim,vector=excluded.vector,'
                    'text_hash=excluded.text_hash,updated_at=excluded.updated_at',
                    (fid, old['model'], old['dim'], vector, old['text_hash'], old['updated_at']))
            if record['index_exists']:
                target_conn.execute('DELETE FROM vec_facts WHERE fact_id=?', (fid,))
                if item['index_b64'] is not None:
                    index = base64.b64decode(item['index_b64'], validate=True)
                    target_conn.execute('INSERT INTO vec_facts(fact_id,embedding) VALUES(?,?)', (fid, index))
        target_conn.commit()
        if target_conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('recovery copy integrity failure')
        return {'ok': True, 'restored': len(ids), 'destination': str(target)}
    except BaseException:
        if target_conn is not None:
            target_conn.rollback()
        if target_conn is not None:
            target_conn.close(); target_conn = None
        target.unlink(missing_ok=True)
        raise
    finally:
        if target_conn is not None:
            target_conn.close()
        if source_conn is not None:
            source_conn.close()
