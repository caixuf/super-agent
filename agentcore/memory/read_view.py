"""Rebuildable in-memory SQLite view of canonical memory, with fenced deltas.

Construction requires an already-installed change journal. It never enables
capture implicitly. The source remains authoritative; recall reuses the normal
engine against an owned memory connection, not a second ranking implementation.
"""
import logging
import sqlite3
import time
import uuid

from . import change_journal as journal, memory_store, vec_index
from .config import get_config,logger_name


class ViewPending(RuntimeError):
    """Retryable: source embeddings or change catch-up are not complete yet."""


class ViewInvalid(RuntimeError):
    """Readiness was withdrawn; rebuild before serving any more queries."""


class MemoryReadView:
    def __init__(self,source=None,*,max_bytes=256*1024*1024,lease_seconds=300,progress=None,
                 max_delta_bytes=64*1024*1024,checkpoint=None):
        if type(max_bytes) is not int or max_bytes<16384:raise ValueError('invalid view memory budget')
        if type(max_delta_bytes) is not int or max_delta_bytes<1024:raise ValueError('invalid delta payload budget')
        self.source=source if source is not None else memory_store._connect()
        self.owns_source=source is None
        self.conn=None;self.consumer=None;self.ready=False;self.invalid=False
        self.max_bytes=max_bytes;self.lease_seconds=lease_seconds
        self.max_delta_bytes=max_delta_bytes;self.checkpoint=checkpoint
        self.binding=self._binding()
        self._renew_at=0
        try:
            self.consumer=journal.register(self.source,'read-view-'+uuid.uuid4().hex,lease_seconds=lease_seconds)
            self.conn=sqlite3.connect(':memory:');self.conn.row_factory=sqlite3.Row
            vec_index.load_into(self.conn)
            page_size=self.source.execute('PRAGMA page_size').fetchone()[0]
            filename=next(row[2] for row in self.source.execute('PRAGMA database_list') if row[1]=='main')
            heartbeat=sqlite3.connect(filename,timeout=10) if filename else None
            next_renew=time.monotonic()+min(lease_seconds/3,30)
            def copying(status,remaining,total):
                nonlocal next_renew
                if total*page_size>max_bytes:raise ViewInvalid('memory_view_snapshot_budget')
                if heartbeat is not None and time.monotonic()>=next_renew:
                    journal.renew(heartbeat,self.consumer)
                    next_renew=time.monotonic()+min(lease_seconds/3,30)
                if progress is not None:progress(status,remaining,total)
            try:
                with journal.read_baseline(self.source,self.consumer) as baseline:
                    self.source.backup(self.conn,pages=256,progress=copying,sleep=.01)
            finally:
                if heartbeat is not None:heartbeat.close()
            self.page_size=self.conn.execute('PRAGMA page_size').fetchone()[0]
            if self.bytes()>max_bytes:raise ViewInvalid('memory_view_snapshot_budget')
            self.conn.execute('PRAGMA max_page_count='+str(max_bytes//self.page_size))
            # Copied capture triggers must not create an independent journal.
            with self.conn:
                for name in journal.TRIGGERS:self.conn.execute('DROP TRIGGER '+name)
                for table in (journal.CONSUMERS,journal.EVENTS,journal.META):self.conn.execute('DROP TABLE '+table)
            self._acknowledge(baseline)
            self._renew_at=time.monotonic()+min(lease_seconds/3,30)
            self.refresh()
        except Exception:
            self.invalid=True;self.close();raise

    def bytes(self):
        return self.conn.execute('PRAGMA page_count').fetchone()[0]*self.conn.execute('PRAGMA page_size').fetchone()[0]

    def _binding(self):
        cfg=get_config()
        return (cfg.default_user,cfg.default_agent,str(cfg.db_path().resolve()))

    def _acknowledge(self,batch):
        # Persist a recovery witness before advancing/pruning the source cursor.
        # Failure leaves the old consumer pin and forbids serving the new view.
        if self.checkpoint is not None:self.checkpoint(self.consumer,batch)
        self.consumer=journal.acknowledge(self.source,self.consumer,batch)

    def _apply(self,batch):
        ids=list(dict.fromkeys(row[2] for row in batch.events))
        if not ids:return
        # Load all three entities from the same source snapshot. Chunking stays
        # below old SQLite's variable limit, even during a large update burst.
        records={table:[] for table in journal.TABLES};payload_bytes=0
        limited=hasattr(self.source,'setlimit')
        previous=self.source.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,self.max_delta_bytes) if limited else None
        try:
            for offset in range(0,len(ids),400):
                chunk=ids[offset:offset+400];marks=','.join('?' for _ in chunk)
                for table in journal.TABLES:
                    select='rowid AS _view_rowid,*' if table=='working_fact' else '*'
                    cursor=self.source.execute('SELECT '+select+' FROM '+table+' WHERE fact_id IN ('+marks+')',chunk)
                    names=[item[0] for item in cursor.description]
                    for row in cursor:
                        payload_bytes+=sum(len(v.encode('utf-8')) if isinstance(v,str) else len(v) if isinstance(v,bytes)
                                           else 0 if v is None else 8 for v in row)
                        if payload_bytes>self.max_delta_bytes:raise ViewInvalid('memory_view_delta_payload_budget')
                        records[table].append(dict(zip(names,row)))
        finally:
            if limited:self.source.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,previous)
        embeds={row['fact_id']:row for row in records['fact_embeddings']}
        for fact in records['working_fact']:
            if fact['status']!='active':continue
            embedding=embeds.get(fact['fact_id'])
            text=((fact['subject'] or '')+' '+(fact['statement'] or '')).strip()
            if (not embedding or embedding['text_hash']!=memory_store._text_hash(text)
                    or embedding['model']!=memory_store.DEFAULT_EMBED_MODEL):
                raise ViewPending('memory_view_embedding_pending')
        indexed=bool(self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='vec_facts'").fetchone())
        with self.conn:
            for offset in range(0,len(ids),400):
                chunk=ids[offset:offset+400];marks=','.join('?' for _ in chunk)
                for table in journal.TABLES:self.conn.execute('DELETE FROM '+table+' WHERE fact_id IN ('+marks+')',chunk)
            for table,rows in records.items():
                if not rows:continue
                columns=list(rows[0]);names=['rowid' if key=='_view_rowid' else key for key in columns]
                quoted=','.join('"'+key.replace('"','""')+'"' for key in names)
                self.conn.executemany('INSERT INTO '+table+' ('+quoted+') VALUES ('+','.join('?' for _ in columns)+')',
                                      [tuple(row[key] for key in columns) for row in rows])
            if indexed:
                existing={row['fact_id'] for row in records['working_fact']}
                for fid in ids:
                    self.conn.execute('DELETE FROM vec_facts WHERE fact_id=?',(fid,))
                    embedding=embeds.get(fid)
                    if fid in existing and embedding and embedding['dim']==get_config().embed_dim:
                        vector=embedding['vector']
                        if len(vector)!=embedding['dim']*4:raise ViewInvalid('memory_view_vector_corrupt')
                        self.conn.execute('INSERT INTO vec_facts(fact_id,embedding) VALUES (?,?)',(fid,vector))
            if self.bytes()>self.max_bytes:raise ViewInvalid('memory_view_delta_budget')

    def refresh(self,*,max_batches=32,max_seconds=5):
        if self.invalid or self.conn is None:raise ViewInvalid('memory_view_rebuild_required')
        self.ready=False;deadline=time.monotonic()+max_seconds
        try:
            if self._binding()!=self.binding:raise ViewInvalid('memory_view_configuration_changed')
            for _ in range(max_batches):
                with journal.read_changes(self.source,self.consumer) as batch:self._apply(batch)
                if batch.events:
                    self._acknowledge(batch)
                    journal.prune(self.source)
                    self._renew_at=time.monotonic()+min(self.lease_seconds/3,30)
                elif time.monotonic()>=self._renew_at:
                    journal.renew(self.source,self.consumer)
                    journal.prune(self.source)
                    self._renew_at=time.monotonic()+min(self.lease_seconds/3,30)
                if batch.caught_up:
                    self.ready=True;return self.consumer.seq
                if time.monotonic()>=deadline:break
            raise ViewPending('memory_view_catchup_budget')
        except ViewPending:raise
        except Exception:
            self.invalid=True;raise

    def recall(self,query='',**kwargs):
        if '_connection' in kwargs:raise ValueError('caller cannot replace the read view')
        self.refresh()
        try:return memory_store.recall_facts(query,**kwargs,_connection=self.conn)
        except Exception:
            self.ready=False;self.invalid=True;raise

    def close(self):
        self.ready=False
        if self.conn is not None:self.conn.close();self.conn=None
        if self.consumer is not None:
            try:journal.release(self.source,self.consumer)
            except Exception as exc:
                logging.getLogger(logger_name('read_view')).warning('view lease cleanup failed: %s',type(exc).__name__)
            self.consumer=None
        if self.owns_source:self.source.close()
