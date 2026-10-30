"""Private Unix-socket recall service using a journal-fed memory view."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import importlib
import json
import os
import queue
from pathlib import Path
import re
import signal
import socket
import stat
import threading
import time

from . import memory_store,read_view,change_journal,recovery_checkpoint
from .recovery_checkpoint import RecoveryCheckpoint,principal

MAX_REQUEST=32768
MAX_REPLY=2*1024*1024
FILTERS={'ebm','tags','kinds','scopes','domains','genres','limit','user_id','include_unknown'}


def build_fingerprint():
    files=[__file__,memory_store.__file__,read_view.__file__,change_journal.__file__,recovery_checkpoint.__file__]
    return hashlib.sha256(b''.join(Path(path).read_bytes() for path in files)).hexdigest()


class RecallUnavailable(RuntimeError):
    def __init__(self,error_type):
        self.error_type=error_type
        super().__init__('resident_recall_unavailable')


def request(path,payload,*,timeout=7):
    data=json.dumps(dict(payload,deadline=time.time()+timeout),ensure_ascii=False).encode()+b'\n'
    if len(data)>MAX_REQUEST:raise ValueError('recall request budget')
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
        client.settimeout(timeout);client.connect(str(path));client.sendall(data)
        with client.makefile('rb') as stream:raw=stream.readline(MAX_REPLY+1)
    if len(raw)>MAX_REPLY or not raw.endswith(b'\n'):raise RecallUnavailable('InvalidResponse')
    reply=json.loads(raw)
    if not isinstance(reply,dict) or reply.get('ok') is not True:
        kind=reply.get('error_type') if isinstance(reply,dict) else None
        raise RecallUnavailable(kind if isinstance(kind,str) and re.fullmatch('[A-Za-z_]{1,64}',kind) else 'BackendError')
    return reply['result']


def validate_request(payload,identity,build):
    if not isinstance(payload,dict) or set(payload)-{'op','query','kwargs','identity','build_sha256','deadline'}:
        raise ValueError('invalid recall envelope')
    deadline=payload.get('deadline')
    if type(deadline) not in (int,float) or not time.time()<deadline<=time.time()+30:
        raise ValueError('expired recall request')
    if payload.get('op')=='health':return
    if payload.get('op')!='recall' or payload.get('identity')!=identity or payload.get('build_sha256')!=build:
        raise ValueError('recall identity or version mismatch')
    query=payload.get('query');kwargs=payload.get('kwargs',{})
    if not isinstance(query,str) or len(query)>8192 or not isinstance(kwargs,dict) or set(kwargs)-FILTERS:
        raise ValueError('invalid recall arguments')
    if kwargs.get('user_id',identity['user_id'])!=identity['user_id']:raise ValueError('recall principal mismatch')
    limit=kwargs.get('limit',5)
    if type(limit) is not int or not 1<=limit<=20:raise ValueError('invalid recall limit')
    for key in ('kinds','scopes','domains','genres'):
        values=kwargs.get(key)
        if values is not None and (not isinstance(values,list) or len(values)>32 or
                any(not isinstance(v,str) or len(v)>160 for v in values)):raise ValueError('invalid recall filter')
    tags=kwargs.get('tags')
    if tags is not None and (not isinstance(tags,dict) or len(tags)>32 or
            any(not isinstance(k,str) or not isinstance(v,str) or len(k)>160 or len(v)>512 for k,v in tags.items())):
        raise ValueError('invalid recall tags')
    if kwargs.get('ebm') is not None and (not isinstance(kwargs['ebm'],str) or len(kwargs['ebm'])>128):
        raise ValueError('invalid recall bucket')
    if 'include_unknown' in kwargs and type(kwargs['include_unknown']) is not bool:raise ValueError('invalid recall flag')


class ServingMemory:
    def __init__(self,checkpoint,*,initialize=False,max_bytes=3*1024**3,max_delta_bytes=64*1024**2,
                 lease_seconds=300,event_sink=None,progress=None):
        self.source=memory_store._connect();self.view=None;self.state='starting';self.error_type=None;self.reason_code=None
        self.identity=principal();self.build=build_fingerprint();self.event_sink=event_sink
        try:
            self.checkpoint=RecoveryCheckpoint(checkpoint,initialize=initialize)
            self.checkpoint.validate(self.source)
            self.view=read_view.MemoryReadView(self.source,max_bytes=max_bytes,max_delta_bytes=max_delta_bytes,
                lease_seconds=lease_seconds,checkpoint=self.checkpoint.save,progress=progress)
            self._state('ready')
        except Exception:self.close();raise

    def _state(self,state,error=None):
        kind=type(error).__name__ if error is not None else None
        # Never expose arbitrary backend exception text in the health response.
        allowed={'memory_view_embedding_pending','memory_view_catchup_budget'}
        reason=str(error) if isinstance(error,read_view.ViewPending) and str(error) in allowed else None
        changed=(state,kind,reason)!=(self.state,self.error_type,self.reason_code)
        self.state=state;self.error_type=kind;self.reason_code=reason
        if changed and self.event_sink is not None:self.event_sink(dict(state=state,error_type=kind,reason_code=reason,at=time.time()))

    def health(self):
        return dict(ready=self.state=='ready' and self.view is not None and self.view.ready,
                    state=self.state,error_type=self.error_type,reason_code=self.reason_code,identity=self.identity,build_sha256=self.build,
                    seq=self.checkpoint.record['seq'] if self.checkpoint.record else None,
                    view_bytes=self.view.bytes() if self.view is not None and self.view.conn is not None else None)

    def maintain(self):
        if self.view is None or self.view.invalid:return
        try:
            # Renew even while an incomplete embedding postpones acknowledgement.
            change_journal.renew(self.source,self.view.consumer)
            self.view.refresh();self._state('ready')
        except read_view.ViewPending as exc:self._state('pending',exc)
        except Exception as exc:
            self.view.ready=False;self.view.invalid=True;self._state('blocked',exc)

    def dispatch(self,payload):
        validate_request(payload,self.identity,self.build)
        if payload['op']=='health':return self.health()
        if self.view is None or self.view.invalid:raise RecallUnavailable('RebuildRequired')
        try:
            answer=self.view.recall(payload['query'],**payload.get('kwargs',{}));self._state('ready');return answer
        except read_view.ViewPending as exc:self._state('pending',exc);raise
        except Exception as exc:self._state('blocked',exc);raise

    def close(self):
        if self.view is not None:self.view.close();self.view=None
        self.source.close();self.state='stopped'


@contextmanager
def private_lock(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if path.parent.stat().st_mode & 0o077:raise ValueError('socket/state directory must be private')
    if path.is_symlink():raise ValueError('lock path redirected')
    with path.open('a') as lock:
        path.chmod(0o600);fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);yield


def serve(engine,path,stop,*,maintenance_seconds=5):
    """Serve bounded clients while all memory operations stay on the owning thread.

    Health is a cached observation: while a recall or maintenance call is in
    progress it reports busy, never a stale ready result. At most one request
    waits for the engine and excess clients receive an explicit busy error.
    """
    path=Path(path)
    if path.is_symlink():raise ValueError('socket path redirected')
    if path.exists():
        if not stat.S_ISSOCK(path.lstat().st_mode):raise ValueError('socket path occupied')
        path.unlink()
    requests=queue.Queue(maxsize=1)
    slots=threading.BoundedSemaphore(16)
    guard=threading.Lock()
    snapshot=engine.health()
    busy=False
    def publish(value,working):
        nonlocal snapshot,busy
        with guard:
            snapshot=value;busy=working
    def client_loop(client):
        try:
            with client:
                client.settimeout(2)
                try:
                    with client.makefile('rb') as stream:raw=stream.readline(MAX_REQUEST+1)
                    if len(raw)>MAX_REQUEST or not raw.endswith(b'\n'):raise ValueError('recall request budget')
                    payload=json.loads(raw)
                    validate_request(payload,engine.identity,engine.build)
                    if payload['op']=='health':
                        with guard:
                            result=dict(snapshot)
                            if busy:result.update(ready=False,state='busy',error_type=None,reason_code=None)
                        reply=dict(ok=True,result=result)
                    else:
                        item=(payload,threading.Event(),{})
                        try:requests.put_nowait(item)
                        except queue.Full:raise RecallUnavailable('Busy')
                        remaining=max(0,min(30,payload['deadline']-time.time()))
                        if not item[1].wait(remaining):raise TimeoutError('recall deadline')
                        reply=item[2]
                except Exception as exc:
                    reply=dict(ok=False,error_type=exc.error_type if isinstance(exc,RecallUnavailable) else type(exc).__name__)
                raw=json.dumps(reply,ensure_ascii=False,default=str).encode()+b'\n'
                if len(raw)>MAX_REPLY:raw=b'{"ok":false,"error_type":"ResponseBudget"}\n'
                try:client.sendall(raw)
                except OSError:pass
        finally:slots.release()
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as listener:
        listener.bind(str(path));path.chmod(0o600);listener.listen(16);listener.settimeout(.2)
        def accept_loop():
            while not stop.is_set():
                try:client,_=listener.accept()
                except socket.timeout:continue
                except OSError:break
                if not slots.acquire(blocking=False):client.close();continue
                threading.Thread(target=client_loop,args=(client,),daemon=True).start()
        acceptor=threading.Thread(target=accept_loop,name='recall-accept',daemon=True);acceptor.start()
        next_maintenance=time.monotonic()+maintenance_seconds
        try:
            while not stop.is_set():
                if time.monotonic()>=next_maintenance:
                    publish(snapshot,True)
                    try:engine.maintain()
                    finally:publish(engine.health(),False)
                    next_maintenance=time.monotonic()+maintenance_seconds
                try:item=requests.get(timeout=.2)
                except queue.Empty:continue
                payload,done,reply=item
                publish(snapshot,True)
                try:
                    # Recheck the deadline after queueing, before any work begins.
                    validate_request(payload,engine.identity,engine.build)
                    reply.update(ok=True,result=engine.dispatch(payload))
                except Exception as exc:
                    reply.update(ok=False,error_type=exc.error_type if isinstance(exc,RecallUnavailable) else type(exc).__name__)
                finally:
                    publish(engine.health(),False);done.set();requests.task_done()
        finally:
            listener.close();acceptor.join(timeout=1);path.unlink(missing_ok=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bootstrap',required=True);p.add_argument('--socket',type=Path,required=True)
    p.add_argument('--checkpoint',type=Path,required=True);p.add_argument('--initialize',action='store_true')
    p.add_argument('--user',required=True);p.add_argument('--agent',required=True)
    p.add_argument('--view-mib',type=int,default=3072);p.add_argument('--delta-mib',type=int,default=64)
    p.add_argument('--address-space-mib',type=int,default=4096)
    p.add_argument('--lease-seconds',type=int,default=300);p.add_argument('--maintenance-seconds',type=float,default=5)
    args=p.parse_args()
    if not 1<=args.maintenance_seconds<=30 or args.maintenance_seconds>=args.lease_seconds/3:
        p.error('maintenance interval must renew before lease expiry')
    if not 16<=args.view_mib<args.address_space_mib<=16384 or not 1<=args.delta_mib<=256:
        p.error('invalid process memory limits')
    import resource
    soft,hard=resource.getrlimit(resource.RLIMIT_AS);budget=args.address_space_mib*1024**2
    if hard!=resource.RLIM_INFINITY:budget=min(budget,hard)
    resource.setrlimit(resource.RLIMIT_AS,(budget,hard))
    module,symbol=args.bootstrap.split(':',1);getattr(importlib.import_module(module),symbol)()
    if principal()['user_id']!=args.user or principal()['agent_id']!=args.agent:p.error('recall principal mismatch')
    stop=threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stop.set())
    def progress(*_):
        if stop.is_set():raise InterruptedError('recall startup cancelled')
    with private_lock(str(args.socket)+'.lock'),private_lock(str(args.checkpoint)+'.lock'):
        engine=None
        try:
            engine=ServingMemory(args.checkpoint,initialize=args.initialize,max_bytes=args.view_mib*1024**2,
                max_delta_bytes=args.delta_mib*1024**2,lease_seconds=args.lease_seconds,progress=progress,
                event_sink=lambda event:print(json.dumps(event),flush=True))
            if not stop.is_set():serve(engine,args.socket,stop,maintenance_seconds=args.maintenance_seconds)
        except InterruptedError:
            if not stop.is_set():raise
        finally:
            if engine is not None:engine.close()


if __name__=='__main__':main()
