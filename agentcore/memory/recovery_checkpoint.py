"""Private recovery witness outside the source database and its backups."""
import hashlib
import json
import os
from pathlib import Path
import tempfile

from . import change_journal as journal
from .config import get_config
from .read_view import ViewInvalid


def principal():
    cfg=get_config()
    return dict(user_id=cfg.default_user,agent_id=cfg.default_agent,
                database_sha256=hashlib.sha256(str(cfg.db_path().resolve()).encode()).hexdigest())


class RecoveryCheckpoint:
    def __init__(self,path,*,initialize=False):
        self.path=Path(path);self.binding=principal();self.record=self._load()
        if self.record is None and not initialize:
            raise ViewInvalid('memory_recovery_checkpoint_missing')

    def _load(self):
        if self.path.is_symlink():raise ViewInvalid('memory_recovery_checkpoint_redirected')
        if not self.path.exists():return None
        info=self.path.stat()
        if info.st_mode & 0o077 or (hasattr(os,'geteuid') and info.st_uid!=os.geteuid()):
            raise ViewInvalid('memory_recovery_checkpoint_permissions')
        with self.path.open('rb') as stream:raw=stream.read(16385)
        if len(raw)>16384:raise ViewInvalid('memory_recovery_checkpoint_budget')
        try:record=json.loads(raw)
        except (ValueError,UnicodeError):raise ViewInvalid('memory_recovery_checkpoint_invalid') from None
        if (not isinstance(record,dict) or set(record)!=
                {'version','binding','source_identity','epoch','schema_version','seq','token'}
                or type(record['version']) is not int or record['version']!=1
                or type(record['seq']) is not int or record['seq']<0
                or type(record['schema_version']) is not int):
            raise ViewInvalid('memory_recovery_checkpoint_invalid')
        return record

    def validate(self,source):
        if principal()!=self.binding:raise ViewInvalid('memory_recovery_principal_changed')
        record=self.record
        with journal._transaction(source):
            meta=journal._validate(source)
            if record is None:return
            if (record['binding']!=self.binding or record['epoch']!=meta['epoch'] or
                    record['source_identity']!=list(journal._identity(source)) or
                    record['schema_version']!=source.execute('PRAGMA schema_version').fetchone()[0]):
                raise ViewInvalid('memory_recovery_source_changed')
            seq=record['seq']
            if not meta['floor']<=seq<=meta['latest']:
                raise ViewInvalid('memory_recovery_history_unavailable_or_rewound')
            if journal._anchor(source,meta,seq)!=record['token']:
                raise ViewInvalid('memory_recovery_history_changed')

    def save(self,consumer,batch):
        record=dict(version=1,binding=self.binding,source_identity=list(consumer.source_identity),
                    epoch=consumer.epoch,schema_version=consumer.schema_version,
                    seq=batch.end_seq,token=batch.end_token)
        if self.record is not None:
            if any(record[k]!=self.record[k] for k in ('binding','source_identity','epoch','schema_version')):
                raise ViewInvalid('memory_recovery_source_changed')
            if record['seq']<self.record['seq'] or (record['seq']==self.record['seq'] and record['token']!=self.record['token']):
                raise ViewInvalid('memory_recovery_checkpoint_regression')
        if self._load()!=self.record:raise ViewInvalid('memory_recovery_checkpoint_changed_externally')
        if record==self.record:return
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if self.path.parent.stat().st_mode & 0o077:
            raise ViewInvalid('memory_recovery_directory_permissions')
        raw=(json.dumps(record,sort_keys=True)+'\n').encode()
        temporary=None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent,delete=False) as stream:
                temporary=Path(stream.name);stream.write(raw);stream.flush();os.fsync(stream.fileno())
            temporary.replace(self.path);temporary=None
            directory=os.open(self.path.parent,os.O_RDONLY)
            try:os.fsync(directory)
            finally:os.close(directory)
        finally:
            if temporary is not None:temporary.unlink(missing_ok=True)
        self.record=record
