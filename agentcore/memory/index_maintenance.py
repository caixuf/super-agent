"""Snapshot-backed removal and isolated recovery of orphan index records.

Facts are never deleted or rewritten. Only records already absent from the
snapshot's fact table are eligible; each is rechecked under the live write lock.
New facts and changed index records are preserved rather than guessed obsolete.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3

from . import vec_index

TABLES = ('fact_embeddings', 'fact_tags', 'vec_facts')


@contextmanager
def connection(path, *, writable=False):
    path = Path(path).resolve(strict=True)
    conn = sqlite3.connect(path.as_uri()+('?mode=rw' if writable else '?mode=ro'), uri=True, timeout=30)
    try:
        loaded = vec_index.load_into(conn)
        schema = conn.execute("SELECT sql FROM sqlite_master WHERE name='vec_facts'").fetchone()
        if schema and 'using vec0' in schema[0].lower() and not loaded:
            raise RuntimeError('vector extension required for maintenance')
        yield conn
    finally:
        conn.close()


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _save(path, value):
    path = Path(path)
    with path.open('x') as stream:
        path.chmod(0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush(); os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_directories(root):
    for directory in sorted([root,*[p for p in root.rglob('*') if p.is_dir() and not p.is_symlink()]],key=lambda p:len(p.parts),reverse=True):
        fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)


def audit(conn):
    # A correlated EXISTS over a virtual vector table can become a full scan
    # per fact. Read the ID sets once; never perform that quadratic query.
    facts = {row[0] for row in conn.execute('SELECT fact_id FROM working_fact')}
    active = {row[0] for row in conn.execute("SELECT fact_id FROM working_fact WHERE status='active'")}
    result = {}
    for table in TABLES:
        ids = {row[0] for row in conn.execute('SELECT fact_id FROM '+table)}
        result[table] = dict(orphan_ids=sorted(ids-facts), active_missing=len(active-ids))
    return dict(fact_count=len(facts), active_count=len(active), indexes=result)


def _rows(conn, table, fact_id):
    if table not in TABLES:
        raise ValueError('unsupported index table')
    columns = [r[1] for r in conn.execute('PRAGMA table_info('+table+')')]
    if not columns or any(not re.fullmatch('[a-zA-Z_][a-zA-Z0-9_]*', c) for c in columns):
        raise ValueError('unsupported index schema')
    return columns, conn.execute('SELECT * FROM '+table+' WHERE fact_id=?', (fact_id,)).fetchall()


def _rows_sha(columns, rows):
    def value(v):
        return {'blob':v.hex()} if isinstance(v,bytes) else v
    encoded = [json.dumps([value(v) for v in row], sort_keys=True, ensure_ascii=False) for row in rows]
    return hashlib.sha256(json.dumps([columns,sorted(encoded)],ensure_ascii=False).encode()).hexdigest()


def snapshot_plan(database, output_dir, *, ancillary_roots=()):
    source = Path(database).resolve(strict=True)
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    snapshot = root/'memory.sqlite'
    with connection(source) as src:
        dst = sqlite3.connect(snapshot)
        try:
            src.backup(dst, pages=2048, sleep=.01)
        finally:
            dst.close()
    snapshot.chmod(0o600)
    with snapshot.open('rb') as stream:
        os.fsync(stream.fileno())
    ancillary=[];total_bytes=0
    for supplied in ancillary_roots:
        directory=Path(supplied).resolve(strict=True)
        if not directory.is_dir() or not directory.is_relative_to(source.parent):
            raise ValueError('ancillary root must be inside the memory data directory')
        if root.is_relative_to(directory):
            raise ValueError('backup destination inside ancillary root')
        for path in sorted(directory.rglob('*')):
            if path.is_symlink():raise ValueError('ancillary symlink refused')
            if path.suffix not in ('.md','.json'):
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(directory):
                raise ValueError('ancillary symlink refused')
            raw=path.read_bytes();total_bytes+=len(raw)
            if len(ancillary)>=4096 or total_bytes>64*1024*1024:
                raise ValueError('ancillary snapshot budget')
            relative=path.relative_to(source.parent)
            target=root/'ancillary'/relative
            target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            with target.open('xb') as stream:
                target.chmod(0o600);stream.write(raw);stream.flush();os.fsync(stream.fileno())
            if path.read_bytes()!=raw:
                raise RuntimeError('ancillary source changed during backup')
            ancillary.append(dict(relative_path=str(relative),bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest()))
    _sync_directories(root)
    with connection(snapshot) as conn:
        if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise RuntimeError('snapshot integrity failed')
        report = audit(conn)
        entries = []
        for table, state in report['indexes'].items():
            for fid in state['orphan_ids']:
                columns, rows = _rows(conn, table, fid)
                entries.append(dict(table=table, fact_id=fid, row_count=len(rows), rows_sha256=_rows_sha(columns,rows)))
    plan = dict(schema='agentcore.orphan_indexes.v1', source=str(source), snapshot=snapshot.name,
                snapshot_sha256=file_sha(snapshot), created_at=datetime.now(timezone.utc).isoformat(),
                audit=report, entries=entries,ancillary=ancillary)
    _save(root/'plan.json', plan)
    return plan


@contextmanager
def _validated_plan(path):
    path = Path(path).resolve(strict=True)
    plan = json.loads(path.read_text())
    if plan.get('schema') != 'agentcore.orphan_indexes.v1' or plan.get('snapshot') != 'memory.sqlite':
        raise ValueError('unsupported maintenance plan')
    snapshot = path.parent/'memory.sqlite'
    if file_sha(snapshot) != plan['snapshot_sha256']:
        raise ValueError('snapshot hash mismatch')
    for item in plan.get('ancillary',[]):
        resource=(path.parent/'ancillary'/item['relative_path']).resolve(strict=True)
        if not resource.is_relative_to(path.parent/'ancillary') or file_sha(resource)!=item['sha256']:
            raise ValueError('ancillary snapshot mismatch')
    with connection(snapshot) as archive:
        for entry in plan['entries']:
            fid, table = entry['fact_id'], entry['table']
            if archive.execute('SELECT 1 FROM working_fact WHERE fact_id=?',(fid,)).fetchone():
                raise ValueError('archived record is not orphaned')
            columns, rows = _rows(archive, table, fid)
            if not rows or _rows_sha(columns,rows) != entry['rows_sha256'] or len(rows)!=entry['row_count']:
                raise ValueError('archived record mismatch')
        yield plan, archive


def restore_ancillary_to_copy(path, destination):
    """Restore verified source/alias resources only into an isolated data tree."""
    target=Path(destination).resolve();target.mkdir(parents=True,exist_ok=True,mode=0o700)
    with _validated_plan(path) as (plan,archive):
        source=Path(plan['source']).resolve().parent
        snapshot_root=Path(path).resolve().parent
        if (target==source or target.is_relative_to(source) or source.is_relative_to(target)
                or target==snapshot_root or target.is_relative_to(snapshot_root) or snapshot_root.is_relative_to(target)):
            raise ValueError('ancillary recovery requires an isolated destination')
        restored=0
        for item in plan.get('ancillary',[]):
            resource=(snapshot_root/'ancillary'/item['relative_path']).resolve(strict=True)
            out=(target/item['relative_path']).resolve()
            if not out.is_relative_to(target):raise ValueError('ancillary recovery path escaped')
            raw=resource.read_bytes()
            if out.exists():
                if file_sha(out)!=item['sha256']:raise ValueError('ancillary recovery collision')
                continue
            out.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            with out.open('xb') as stream:
                out.chmod(0o600);stream.write(raw);stream.flush();os.fsync(stream.fileno())
            restored+=1
        _sync_directories(target)
        return dict(restored_files=restored)


def archive_bundle(path,destination):
    """Copy the verified bundle into a fresh durable backup directory."""
    path=Path(path).resolve(strict=True);target=Path(destination).resolve()
    if target.is_relative_to(path.parent):raise ValueError('archive must be independent of its source bundle')
    with _validated_plan(path) as (plan,archive):
        target.mkdir(parents=True,exist_ok=False,mode=0o700)
        files=[(path,'plan.json'),(path.parent/'memory.sqlite','memory.sqlite')]
        files.extend((path.parent/'ancillary'/i['relative_path'],str(Path('ancillary')/i['relative_path']))
                     for i in plan.get('ancillary',[]))
        for source,name in files:
            out=target/name;out.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            shutil.copyfile(source,out);out.chmod(0o600)
            with out.open('rb') as stream:os.fsync(stream.fileno())
        _sync_directories(target)
    with _validated_plan(target/'plan.json'):
        pass
    return dict(bundle_path=str(target),snapshot_sha256=plan['snapshot_sha256'],ancillary_files=len(plan.get('ancillary',[])))


def apply_plan(path, *, destination=None):
    with _validated_plan(path) as (plan, archive):
        source = Path(destination or plan['source']).resolve(strict=True)
        if os.path.samefile(source, Path(path).resolve().parent/'memory.sqlite'):
            raise ValueError('refusing to modify the archived snapshot')
        outcome = dict(deleted_rows=0, skipped_reattached=0, skipped_changed=0, already_absent=0)
        with connection(source, writable=True) as conn:
            try:
                conn.execute('BEGIN IMMEDIATE')
                for entry in plan['entries']:
                    fid, table = entry['fact_id'], entry['table']
                    if conn.execute('SELECT 1 FROM working_fact WHERE fact_id=?',(fid,)).fetchone():
                        outcome['skipped_reattached'] += 1
                        continue
                    columns, rows = _rows(conn,table,fid)
                    if not rows:
                        outcome['already_absent'] += 1
                    elif _rows_sha(columns,rows) != entry['rows_sha256']:
                        outcome['skipped_changed'] += 1
                    else:
                        conn.execute('DELETE FROM '+table+' WHERE fact_id=?',(fid,))
                        outcome['deleted_rows'] += len(rows)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return outcome


def restore_indexes_to_copy(path, destination):
    """Recovery rehearsal only. Never restore an entire old DB onto a live one."""
    target = Path(destination).resolve(strict=True)
    with _validated_plan(path) as (plan, archive):
        source = Path(plan['source']).resolve()
        if (target == source or (source.exists() and os.path.samefile(target, source))
                or os.path.samefile(target, Path(path).resolve().parent/'memory.sqlite')):
            raise ValueError('recovery destination must be an independent copy')
        restored = 0
        with connection(target,writable=True) as conn:
            try:
                conn.execute('BEGIN IMMEDIATE')
                for entry in plan['entries']:
                    fid, table = entry['fact_id'], entry['table']
                    if conn.execute('SELECT 1 FROM working_fact WHERE fact_id=?',(fid,)).fetchone():
                        raise ValueError('recovery would replace a current fact index')
                    columns, rows = _rows(archive,table,fid)
                    target_columns, existing = _rows(conn,table,fid)
                    if target_columns != columns:
                        raise ValueError('recovery schema mismatch')
                    if existing:
                        if _rows_sha(columns,existing) != entry['rows_sha256']:
                            raise ValueError('recovery index collision')
                        continue
                    marks=','.join('?' for _ in columns)
                    conn.executemany('INSERT INTO '+table+'('+','.join(columns)+') VALUES('+marks+')',rows)
                    restored += len(rows)
                conn.commit()
            except BaseException:
                conn.rollback(); raise
        return dict(restored_rows=restored)
