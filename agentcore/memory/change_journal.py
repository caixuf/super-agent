"""Transactional change capture for a rebuildable memory read view.

Opt-in only: installation never happens on import or on ordinary memory writes.
Triggers cover all writers of facts, tags and embeddings. Consumer leases pin
unapplied events; a bounded full log rejects a mutation instead of losing it.
"""
from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import re
import time
import uuid


TABLES=('working_fact','fact_tags','fact_embeddings')
META='memory_view_meta'
EVENTS='memory_view_changes'
CONSUMERS='memory_view_consumers'


class JournalError(RuntimeError):
    """The view must stop serving and rebuild or resolve the source fault."""


SCHEMAS={
    META:'''CREATE TABLE memory_view_meta (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), epoch TEXT NOT NULL,
        source_schema TEXT NOT NULL, latest INTEGER NOT NULL, latest_token TEXT NOT NULL,
        floor INTEGER NOT NULL, floor_token TEXT NOT NULL,
        max_events INTEGER NOT NULL, max_consumers INTEGER NOT NULL)''',
    EVENTS:'''CREATE TABLE memory_view_changes (
        seq INTEGER PRIMARY KEY, token TEXT NOT NULL, fact_id TEXT NOT NULL,
        entity TEXT NOT NULL, operation TEXT NOT NULL)''',
    CONSUMERS:'''CREATE TABLE memory_view_consumers (
        name TEXT PRIMARY KEY, owner TEXT NOT NULL, cursor INTEGER NOT NULL,
        token TEXT NOT NULL, lease_until REAL NOT NULL)''',
}


def _emit(entity, operation, value, condition='1'):
    return f'''
    SELECT CASE WHEN ({condition}) AND ({value} IS NULL OR typeof({value})!='text'
        OR length({value})=0 OR length(CAST({value} AS BLOB))>2048)
        THEN RAISE(ABORT,'memory_view_invalid_identity') END;
    SELECT CASE WHEN ({condition}) AND
        (SELECT latest-floor>=max_events FROM {META} WHERE singleton=1)
        THEN RAISE(ABORT,'memory_view_change_capacity') END;
    UPDATE {META} SET latest=latest+1,latest_token=lower(hex(randomblob(16)))
        WHERE singleton=1 AND ({condition});
    INSERT INTO {EVENTS}(seq,token,fact_id,entity,operation)
        SELECT latest,latest_token,{value},'{entity}','{operation}' FROM {META}
        WHERE singleton=1 AND ({condition});
    '''


def _triggers():
    result={}
    for table in TABLES:
        for event,suffix,body in [
            ('INSERT','ai',_emit(table,'insert','NEW.fact_id')),
            ('DELETE','ad',_emit(table,'delete','OLD.fact_id')),
            ('UPDATE','au',_emit(table,'update_old','OLD.fact_id','OLD.fact_id IS NOT NEW.fact_id')+
                           _emit(table,'update','NEW.fact_id'))]:
            name='memory_view_'+table+'_'+suffix
            result[name]=f'CREATE TRIGGER {name} AFTER {event} ON {table} BEGIN {body} END'
    return result


TRIGGERS=_triggers()


def _normalized(sql):
    return ' '.join(sql.split()).rstrip(';')


def _source_schema(conn):
    columns={table:[tuple(row) for row in conn.execute('PRAGMA table_info('+table+')')] for table in TABLES}
    if any(not rows or not any(row[1]=='fact_id' for row in rows) for rows in columns.values()):
        raise JournalError('memory_view_source_schema_missing')
    # Unique indices can change REPLACE/delete behavior even without changing
    # columns. A new source constraint requires a reviewed capture migration.
    indices={table:[tuple(row) for row in conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name=? ORDER BY name",(table,))]
        for table in TABLES}
    return hashlib.sha256(json.dumps(dict(columns=columns,indices=indices),sort_keys=True).encode()).hexdigest()


def _identity(conn):
    filename=next(row[2] for row in conn.execute('PRAGMA database_list') if row[1]=='main')
    if not filename:return ('memory',id(conn))
    path=Path(filename).resolve(strict=True);info=path.stat()
    return (str(path),info.st_dev,info.st_ino)


def _validate(conn):
    for name,expected in {**SCHEMAS,**TRIGGERS}.items():
        row=conn.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()
        if row is None or _normalized(row[0])!=_normalized(expected):
            raise JournalError('memory_view_capture_schema_changed')
    row=conn.execute('SELECT * FROM '+META+' WHERE singleton=1').fetchone()
    if row is None:raise JournalError('memory_view_generation_missing')
    names=[d[0] for d in conn.execute('SELECT * FROM '+META+' LIMIT 0').description]
    meta=dict(zip(names,row))
    if (any(type(meta[k]) is not int for k in ('latest','floor','max_events','max_consumers'))
            or not 4<=meta['max_events']<=1000000 or not 1<=meta['max_consumers']<=64
            or any(not isinstance(meta[k],str) or not re.fullmatch('[a-f0-9]{32}',meta[k])
                   for k in ('epoch','latest_token','floor_token'))):
        raise JournalError('memory_view_invalid_metadata')
    if meta['source_schema']!=_source_schema(conn) or not 0<=meta['floor']<=meta['latest']:
        raise JournalError('memory_view_source_schema_or_sequence_changed')
    if _anchor(conn,meta,meta['latest'])!=meta['latest_token']:
        raise JournalError('memory_view_head_changed')
    return meta


def _anchor(conn,meta,seq):
    if seq==meta['floor']:return meta['floor_token']
    row=conn.execute('SELECT token FROM '+EVENTS+' WHERE seq=?',(seq,)).fetchone()
    if row is None:raise JournalError('memory_view_change_gap')
    return row[0]


@contextmanager
def _transaction(conn,write=False):
    if conn.in_transaction:raise JournalError('memory_view_requires_own_transaction')
    conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
    try:
        yield
        if write:conn.commit()
    finally:
        if conn.in_transaction:conn.rollback()


def install(conn,*,max_events=65536,max_consumers=32):
    if type(max_events) is not int or not 4<=max_events<=1000000:
        raise ValueError('invalid journal event budget')
    if type(max_consumers) is not int or not 1<=max_consumers<=64:
        raise ValueError('invalid journal consumer budget')
    with _transaction(conn,True):
        names=set(row[0] for row in conn.execute('SELECT name FROM sqlite_master'))
        if names & set(SCHEMAS):
            meta=_validate(conn)
            if (meta['max_events'],meta['max_consumers'])!=(max_events,max_consumers):
                raise JournalError('memory_view_budget_change_requires_migration')
            return meta['epoch']
        schema=_source_schema(conn);epoch=uuid.uuid4().hex
        for sql in SCHEMAS.values():conn.execute(sql)
        conn.execute('INSERT INTO '+META+' VALUES (1,?,?,0,?,0,?,?,?)',
                     (epoch,schema,epoch,epoch,max_events,max_consumers))
        for sql in TRIGGERS.values():conn.execute(sql)
        _validate(conn)
        return epoch


@dataclass(frozen=True)
class Consumer:
    name:str
    owner:str
    epoch:str
    seq:int
    token:str
    source_identity:tuple
    schema_version:int
    lease_seconds:float


@dataclass(frozen=True)
class Batch:
    consumer:str
    owner:str
    epoch:str
    start_seq:int
    start_token:str
    end_seq:int
    end_token:str
    head_seq:int
    events:tuple
    baseline:bool=False

    @property
    def caught_up(self):return self.end_seq==self.head_seq


def register(conn,name,*,lease_seconds=300,now=None):
    if not isinstance(name,str) or not 1<=len(name)<=128:raise ValueError('invalid consumer name')
    if not 1<=lease_seconds<=3600:raise ValueError('invalid consumer lease')
    now=time.time() if now is None else now
    with _transaction(conn,True):
        meta=_validate(conn)
        conn.execute('DELETE FROM '+CONSUMERS+' WHERE lease_until<=?',(now,))
        if conn.execute('SELECT 1 FROM '+CONSUMERS+' WHERE name=?',(name,)).fetchone():
            raise JournalError('memory_view_consumer_already_owned')
        if conn.execute('SELECT count(*) FROM '+CONSUMERS).fetchone()[0]>=meta['max_consumers']:
            raise JournalError('memory_view_consumer_capacity')
        owner=uuid.uuid4().hex
        conn.execute('INSERT INTO '+CONSUMERS+' VALUES (?,?,?,?,?)',
                     (name,owner,meta['latest'],meta['latest_token'],now+lease_seconds))
        return Consumer(name,owner,meta['epoch'],meta['latest'],meta['latest_token'],
                        _identity(conn),conn.execute('PRAGMA schema_version').fetchone()[0],lease_seconds)


def _check(conn,consumer,now):
    meta=_validate(conn)
    if (_identity(conn)!=consumer.source_identity or meta['epoch']!=consumer.epoch or
            conn.execute('PRAGMA schema_version').fetchone()[0]!=consumer.schema_version):
        raise JournalError('memory_view_source_replaced_or_schema_changed')
    row=conn.execute('SELECT owner,cursor,token,lease_until FROM '+CONSUMERS+' WHERE name=?',
                     (consumer.name,)).fetchone()
    if not row or row[0]!=consumer.owner or row[3]<=now:
        raise JournalError('memory_view_consumer_lease_lost')
    if (row[1],row[2])!=(consumer.seq,consumer.token):
        raise JournalError('memory_view_consumer_cursor_changed')
    if not meta['floor']<=consumer.seq<=meta['latest'] or _anchor(conn,meta,consumer.seq)!=consumer.token:
        raise JournalError('memory_view_cursor_gap_or_history_changed')
    return meta


@contextmanager
def read_baseline(conn,consumer,*,now=None):
    """Copy all source rows inside this snapshot, then acknowledge its head.

    Registration pins events before this scan starts. Writes during the copy
    remain in the log and are applied before the view serves its first query.
    """
    now=time.time() if now is None else now
    with _transaction(conn):
        meta=_check(conn,consumer,now)
        yield Batch(consumer.name,consumer.owner,consumer.epoch,consumer.seq,consumer.token,
                    meta['latest'],meta['latest_token'],meta['latest'],(),True)


@contextmanager
def read_changes(conn,consumer,*,limit=4096,now=None):
    """Keep this snapshot open while loading dirty facts, tags and embeddings.

    A caller must not publish a partially caught-up view. Apply the returned
    rows transactionally to the view before acknowledging the batch.
    """
    if type(limit) is not int or not 1<=limit<=65536:raise ValueError('invalid change batch budget')
    now=time.time() if now is None else now
    with _transaction(conn):
        meta=_check(conn,consumer,now)
        rows=conn.execute('SELECT seq,token,fact_id,entity,operation FROM '+EVENTS+
                          ' WHERE seq>? ORDER BY seq LIMIT ?',(consumer.seq,limit)).fetchall()
        if (len(rows)!=min(limit,meta['latest']-consumer.seq) or
                any(row[0]!=consumer.seq+i+1 for i,row in enumerate(rows))):
            raise JournalError('memory_view_change_gap')
        end,token=(rows[-1][0],rows[-1][1]) if rows else (consumer.seq,consumer.token)
        yield Batch(consumer.name,consumer.owner,consumer.epoch,consumer.seq,consumer.token,
                    end,token,meta['latest'],tuple(tuple(row) for row in rows))


def acknowledge(conn,consumer,batch,*,now=None):
    now=time.time() if now is None else now
    with _transaction(conn,True):
        meta=_check(conn,consumer,now)
        if ((batch.consumer,batch.owner,batch.epoch,batch.start_seq,batch.start_token)!=
                (consumer.name,consumer.owner,consumer.epoch,consumer.seq,consumer.token) or
                not consumer.seq<=batch.end_seq<=batch.head_seq<=meta['latest'] or
                _anchor(conn,meta,batch.end_seq)!=batch.end_token):
            raise JournalError('memory_view_invalid_acknowledgement')
        conn.execute('UPDATE '+CONSUMERS+' SET cursor=?,token=?,lease_until=? WHERE name=? AND owner=?',
                     (batch.end_seq,batch.end_token,now+consumer.lease_seconds,consumer.name,consumer.owner))
        return replace(consumer,seq=batch.end_seq,token=batch.end_token)


def renew(conn,consumer,*,now=None):
    now=time.time() if now is None else now
    with _transaction(conn,True):
        _check(conn,consumer,now)
        conn.execute('UPDATE '+CONSUMERS+' SET lease_until=? WHERE name=? AND owner=?',
                     (now+consumer.lease_seconds,consumer.name,consumer.owner))


def release(conn,consumer,*,now=None):
    now=time.time() if now is None else now
    with _transaction(conn,True):
        _check(conn,consumer,now)
        conn.execute('DELETE FROM '+CONSUMERS+' WHERE name=? AND owner=?',(consumer.name,consumer.owner))


def prune(conn,*,retain_events=128,now=None):
    if type(retain_events) is not int or retain_events<0:raise ValueError('invalid retention budget')
    now=time.time() if now is None else now
    with _transaction(conn,True):
        meta=_validate(conn)
        conn.execute('DELETE FROM '+CONSUMERS+' WHERE lease_until<=?',(now,))
        row=conn.execute('SELECT min(cursor) FROM '+CONSUMERS).fetchone()
        keep=min(retain_events,meta['max_events']//4)
        target=max(meta['floor'],(row[0] if row[0] is not None else meta['latest'])-keep)
        if target>meta['floor']:
            token=_anchor(conn,meta,target)
            count=conn.execute('SELECT count(*) FROM '+EVENTS+' WHERE seq>? AND seq<=?',(meta['floor'],target)).fetchone()[0]
            if count!=target-meta['floor']:raise JournalError('memory_view_change_gap')
            conn.execute('DELETE FROM '+EVENTS+' WHERE seq<=?',(target,))
            conn.execute('UPDATE '+META+' SET floor=?,floor_token=? WHERE singleton=1',(target,token))
        return dict(floor=target,latest=meta['latest'],remaining=meta['latest']-target)
