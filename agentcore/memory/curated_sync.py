"""Index curated Markdown into the canonical fact store without rewriting it.

New sections remain candidates until embeddings are durable and the source is
still the same snapshot. Publication and retirement share one SQLite transaction;
old sections are deprecated, never deleted. No language model rewrites the text.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re

from .config import get_config
from . import memory_store


def sections(text):
    result = []
    for block in re.split(r'^\s*§\s*$', text, flags=re.MULTILINE):
        block = block.strip()
        if not block:
            continue
        match = re.match(r'【([^】]+)】', block)
        if not match or len(block) > 12000:
            raise ValueError('invalid_curated_section')
        title = match.group(1).strip()
        if not title or len(title) > 160:
            raise ValueError('invalid_curated_subject')
        if any(existing['subject'] == title for existing in result):
            raise ValueError('duplicate_curated_subject')
        result.append({'subject': title, 'statement': block})
    if not result or len(result) > 128:
        raise ValueError('curated_section_budget')
    return result


def sync_document(path, *, document_id, flush_timeout=120, aliases=None, expected_revision=None):
    cfg = get_config()
    source = Path(path).resolve(strict=True)
    if not source.is_file() or source.stat().st_size > 512*1024:
        raise ValueError('curated_document_budget')
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError('stable document identity required')
    key = hashlib.sha256((cfg.default_user+'\n'+document_id).encode()).hexdigest()
    archive = cfg.base_dir()/'curated-snapshots'/key
    archive.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(archive/'sync.lock', 'a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        raw = source.read_bytes()
        if len(raw) > 512*1024:
            raise ValueError('curated_document_budget')
        revision = hashlib.sha256(raw).hexdigest()
        if expected_revision is not None and revision != expected_revision:
            raise ValueError('curated_revision_mismatch')
        entries = sections(raw.decode('utf-8'))
        aliases = aliases or {}
        subjects = {entry['subject'] for entry in entries}
        if not isinstance(aliases, dict) or set(aliases) - subjects:
            raise ValueError('invalid_curated_alias_subject')
        for values in aliases.values():
            if (not isinstance(values, list) or len(values) > 8
                    or any(not isinstance(v, str) or not v.strip() or len(v) > 160 for v in values)):
                raise ValueError('invalid_curated_aliases')
            keys = [re.sub(r'[^a-z0-9_\u4e00-\u9fff]', '', v.lower()) for v in values]
            if any(not key for key in keys) or len(set(keys)) != len(keys):
                raise ValueError('invalid_curated_alias_keys')
        alias_bytes = json.dumps(aliases, ensure_ascii=False, sort_keys=True).encode()
        alias_revision = hashlib.sha256(alias_bytes).hexdigest()
        alias_snapshot = archive/(revision+'.aliases-'+alias_revision+'.json')
        if not alias_snapshot.exists():
            with alias_snapshot.open('xb') as stream:
                os.chmod(alias_snapshot, 0o600)
                stream.write(alias_bytes); stream.flush(); os.fsync(stream.fileno())
        elif alias_snapshot.read_bytes() != alias_bytes:
            raise ValueError('curated_alias_snapshot_conflict')
        snapshot = archive/(revision+'.md')
        if not snapshot.exists():
            with snapshot.open('xb') as stream:
                os.chmod(snapshot, 0o600)
                stream.write(raw);stream.flush();os.fsync(stream.fileno())
            directory = os.open(archive, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        elif snapshot.read_bytes() != raw:
            raise ValueError('curated_snapshot_conflict')
        origin = 'curated_profile:'+key
        ids = []
        for entry in entries:
            digest = hashlib.sha256((key+'\n'+entry['subject']+'\n'+entry['statement']).encode()).hexdigest()
            fid = 'curated_'+digest
            result = memory_store.write_fact(**entry, fact_id=fid, source=origin,
                details=json.dumps({'document_id': document_id, 'snapshot_sha256': revision}),
                tags={'scope': 'platform', 'curated_document': key}, initial_status='candidate')
            if not result.get('ok') or result.get('fact_id') != fid:
                raise RuntimeError('curated_staging_failed')
            ids.append(fid)
            # Retry an earlier interrupted staging run whose vector was never saved.
            if not result.get('written') and not memory_store._get_persisted_embeddings([fid]):
                memory_store._submit_background(memory_store._bg_embed, fid, entry['subject'], entry['statement'])
        if memory_store.flush_background(timeout=flush_timeout) != 0:
            return {'ok': False, 'reason': 'curated_index_pending', 'revision': revision}
        persisted = memory_store._get_persisted_embeddings(ids)
        if len(persisted) != len(ids):
            return {'ok': False, 'reason': 'curated_index_incomplete', 'revision': revision}
        if source.read_bytes() != raw:
            return {'ok': False, 'reason': 'curated_source_changed', 'revision': revision}
        conn = memory_store._connect()
        try:
            from . import vec_index
            vector_backend = 'python'
            if vec_index.load_into(conn):
                vector_backend = 'sqlite_vec'
                marks = ','.join('?' for _ in ids)
                indexed = {row[0] for row in conn.execute(
                    'SELECT fact_id FROM vec_facts WHERE fact_id IN ('+marks+')', ids)}
                missing = [fid for fid in ids if fid not in indexed]
                if missing:
                    vec_index.upsert(conn, [(fid, persisted[fid][0]) for fid in missing], dim=cfg.embed_dim)
                coverage = conn.execute('SELECT count(*) FROM vec_facts WHERE fact_id IN ('+marks+')', ids).fetchone()[0]
                if coverage != len(ids):
                    return {'ok': False, 'reason': 'curated_vector_index_incomplete', 'revision': revision}
            with conn:
                conn.execute('CREATE TABLE IF NOT EXISTS curated_documents ('
                             'user_id TEXT NOT NULL, document_id TEXT NOT NULL, revision TEXT NOT NULL,'
                             "fact_ids TEXT NOT NULL, alias_revision TEXT NOT NULL DEFAULT '', PRIMARY KEY(user_id,document_id))")
                if 'alias_revision' not in {r[1] for r in conn.execute('PRAGMA table_info(curated_documents)')}:
                    conn.execute("ALTER TABLE curated_documents ADD COLUMN alias_revision TEXT NOT NULL DEFAULT ''")
                marks = ','.join('?' for _ in ids)
                retired = conn.execute(
                    "UPDATE working_fact SET status='deprecated',fact_chain_summary='curated_source_revised'"
                    " WHERE user_id=? AND source=? AND status='active' AND fact_id NOT IN ("+marks+')',
                    [cfg.default_user, origin, *ids]).rowcount
                conn.execute("UPDATE working_fact SET status='active' WHERE user_id=? AND source=? AND fact_id IN ("+marks+')',
                             [cfg.default_user, origin, *ids])
                for fid, entry in zip(ids, entries):
                    conn.execute("DELETE FROM fact_tags WHERE fact_id=? AND key GLOB 'subject_alias_[0-7]'", (fid,))
                    for i, alias in enumerate(aliases.get(entry['subject'], [])):
                        normalized = re.sub(r'[^a-z0-9_\u4e00-\u9fff]', '', alias.lower())
                        if not normalized:
                            raise ValueError('invalid_curated_alias_key')
                        conn.execute('INSERT INTO fact_tags(fact_id,key,value) VALUES(?,?,?)',
                                     (fid, 'subject_alias_'+str(i), normalized))
                conn.execute('INSERT INTO curated_documents(user_id,document_id,revision,fact_ids,alias_revision) VALUES(?,?,?,?,?) '
                             'ON CONFLICT(user_id,document_id) DO UPDATE SET revision=excluded.revision,'
                             'fact_ids=excluded.fact_ids,alias_revision=excluded.alias_revision',
                             (cfg.default_user, document_id, revision, json.dumps(ids),alias_revision))
        finally:
            conn.close()
        return {'ok': True, 'revision': revision, 'fact_ids': ids, 'retired': retired,
                'snapshot': str(snapshot), 'vector_backend': vector_backend,
                'alias_revision': alias_revision, 'alias_snapshot': str(alias_snapshot)}


def document_manifest(document_id):
    """The exact published source and alias versions, without conversation text."""
    cfg=get_config();conn=memory_store._connect()
    try:
        columns={r[1] for r in conn.execute('PRAGMA table_info(curated_documents)')}
        if not columns:return None
        alias='alias_revision' if 'alias_revision' in columns else "'' AS alias_revision"
        row=conn.execute('SELECT revision,fact_ids,'+alias+' FROM curated_documents WHERE user_id=? AND document_id=?',
                         (cfg.default_user,document_id)).fetchone()
        if row is None:return None
        return dict(document_id=document_id,revision=row['revision'],alias_revision=row['alias_revision'] or None,
                    fact_ids=json.loads(row['fact_ids']))
    finally:conn.close()


def restore_document_revision(*,document_id,revision,alias_revision=None,flush_timeout=120):
    """Republish archived facts only; unrelated and concurrent writes stay intact.

    This does not overwrite the bounded prompt file or replace a live database.
    Rehearse restoration in an isolated configured store before production use.
    """
    if revision is None or not isinstance(document_id,str) or not document_id.strip():
        raise ValueError('invalid_curated_revision')
    for value in (revision,alias_revision):
        if value is not None and (not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value)):
            raise ValueError('invalid_curated_revision')
    cfg=get_config();key=hashlib.sha256((cfg.default_user+'\n'+document_id).encode()).hexdigest()
    archive=cfg.base_dir()/'curated-snapshots'/key
    source=archive/(revision+'.md')
    if not source.resolve(strict=True).is_relative_to(archive.resolve()):
        raise ValueError('curated_snapshot_path_escaped')
    aliases={}
    if alias_revision is not None:
        metadata=archive/(revision+'.aliases-'+alias_revision+'.json')
        if not metadata.resolve(strict=True).is_relative_to(archive.resolve()):
            raise ValueError('curated_alias_path_escaped')
        raw=metadata.read_bytes();aliases=json.loads(raw)
        canonical=json.dumps(aliases,ensure_ascii=False,sort_keys=True).encode()
        if hashlib.sha256(raw).hexdigest()!=alias_revision or raw!=canonical:
            raise ValueError('curated_alias_revision_mismatch')
    return sync_document(source,document_id=document_id,flush_timeout=flush_timeout,
                         aliases=aliases,expected_revision=revision)
