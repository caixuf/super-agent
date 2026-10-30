"""Independent, model-free recall health observations into the existing outbox.

No sender is constructed here. Faults and recoveries stay durable until an
explicitly configured delivery worker obtains a destination receipt.
"""
import argparse
from contextlib import contextmanager
import importlib
import json
import os
from pathlib import Path
import signal
import threading

from agentcore.ops_alerts import AlertOutbox
from agentcore.pool import Pool
from . import recall_service


class BoundedAlertPool(Pool):
    """Dedicated observer store; never impose these limits on a task queue."""
    @contextmanager
    def db(self):
        wal = self.root / 'pool.db-wal'
        if wal.exists() and wal.stat().st_size > 16 * 1024**2:
            raise RuntimeError('health_queue_wal_budget')
        with super().db() as db:
            page_size = db.execute('PRAGMA page_size').fetchone()[0]
            if db.execute('PRAGMA page_count').fetchone()[0] * page_size > 32 * 1024**2:
                raise RuntimeError('health_queue_page_budget')
            db.execute('PRAGMA max_page_count=' + str(32 * 1024**2 // page_size))
            yield db


def _number(value):
    return type(value) is int and 0 <= value <= 2**63-1


def conditions(health, *, identity, build, error=None, resources=None, resource_error=None,
               memory_max=4*1024**3):
    """Whitelist observations. Provider bodies, paths and memory text never pass."""
    answer = {}
    valid = (isinstance(health,dict) and type(health.get('ready')) is bool
             and health.get('state') in ('ready','pending','busy','blocked','starting','stopped')
             and (health.get('seq') is None or _number(health.get('seq'))))
    state = health['state'] if valid else 'unavailable'
    observation = dict(state=state,request_failed=error is not None or not valid)
    if valid and _number(health.get('seq')): observation['seq'] = health['seq']
    if valid and health.get('reason_code') in ('memory_view_embedding_pending','memory_view_catchup_budget'):
        observation['reason_code'] = health['reason_code']
    ready = valid and health['ready'] and state == 'ready'
    answer['recall.availability'] = (ready,dict(ready=True),observation,state=='blocked')
    if valid:
        binding = health.get('identity') == identity
        version = health.get('build_sha256') == build
        answer['recall.binding'] = (binding and version,dict(binding_match=True,build_match=True),
            dict(binding_match=binding,build_match=version),not (binding and version))
    if resources is not None or resource_error is not None:
        resource_ok = isinstance(resources,dict) and resource_error is None
        limits = {key:resources.get(key) if resource_ok else None
                  for key in ('memory_max','memory_high','oom_kill','capture_events','capture_capacity')}
        running = resource_ok and resources.get('running') is True
        bounded = (running and limits['memory_max']==memory_max and _number(limits['memory_high'])
                   and 0 < limits['memory_high'] <= memory_max and limits['oom_kill']==0)
        observed = dict(running=bool(running),resource_probe_failed=not resource_ok)
        observed.update({key:value for key,value in limits.items()
                         if key in ('memory_max','memory_high','oom_kill') and _number(value)})
        answer['recall.resources'] = (bounded,dict(memory_max=memory_max,oom_kill=0),observed,
            running and (limits['memory_max']!=memory_max or (_number(limits['oom_kill']) and limits['oom_kill']>0)))
        installed = resource_ok and resources.get('capture_installed') is True
        used,capacity = limits['capture_events'],limits['capture_capacity']
        valid_counts = _number(used) and _number(capacity) and capacity > 0 and used <= capacity
        healthy = installed and valid_counts and used*5 < capacity*4
        detail = dict(capture_installed=bool(installed))
        if valid_counts: detail.update(events=used,capacity=capacity)
        answer['recall.capture'] = (healthy,dict(capture_installed=True,below_percent=80),detail,
            (resource_ok and not installed) or (valid_counts and used*20 >= capacity*19))
    return answer


def sample(outbox, endpoint, *, profile, identity, build, resource_probe=None,
           fault_grace=30, recovery_grace=10, memory_max=4*1024**3, now=None):
    health = None; error = None; resources = None; resource_error = None
    try: health = recall_service.request(endpoint,dict(op='health'),timeout=2)
    except Exception as exc: error = type(exc).__name__
    if resource_probe is not None:
        try:
            resources = resource_probe()
            if not isinstance(resources,dict): raise ValueError('invalid resource observation')
        except Exception as exc: resource_error = type(exc).__name__
    observations = conditions(health,identity=identity,build=build,error=error,resources=resources,
                              resource_error=resource_error,memory_max=memory_max)
    created = []; statuses = {}
    for scope,(healthy,expected,observed,urgent) in observations.items():
        result = outbox.observe_health(profile,scope,healthy=bool(healthy),expected=expected,
            observed=observed,fault_grace=0 if urgent else fault_grace,
            recovery_grace=recovery_grace,now=now)
        statuses[scope] = bool(healthy)
        if result['created']: created.append(result['event']['event_id'])
    return dict(healthy=statuses,events_created=created)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bootstrap',required=True)
    parser.add_argument('--socket',type=Path,required=True)
    parser.add_argument('--queue',type=Path,required=True)
    parser.add_argument('--profile',required=True)
    parser.add_argument('--resource-probe')
    parser.add_argument('--interval',type=float,default=10)
    parser.add_argument('--fault-grace',type=float,default=30)
    parser.add_argument('--recovery-grace',type=float,default=10)
    parser.add_argument('--memory-max',type=int,default=4*1024**3)
    args = parser.parse_args()
    if (not 1 <= args.interval <= 60 or not 0 <= args.fault_grace <= 3600
            or not 0 <= args.recovery_grace <= 3600 or not 16*1024**2 <= args.memory_max <= 16*1024**3
            or not args.queue.is_absolute() or not args.socket.is_absolute()):
        parser.error('invalid health observer limits or paths')
    os.umask(0o077)
    module,symbol = args.bootstrap.split(':',1)
    getattr(importlib.import_module(module),symbol)()
    probe = None
    if args.resource_probe:
        module,symbol = args.resource_probe.split(':',1)
        probe = getattr(importlib.import_module(module),symbol)
    stop = threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT): signal.signal(sig,lambda *_:stop.set())
    with recall_service.private_lock(args.queue / 'observer.lock'):
        pool = BoundedAlertPool(args.queue); outbox = AlertOutbox(str(args.queue),pool=pool)
        while not stop.is_set():
            result = sample(outbox,args.socket,profile=args.profile,identity=recall_service.principal(),
                build=recall_service.build_fingerprint(),resource_probe=probe,
                fault_grace=args.fault_grace,recovery_grace=args.recovery_grace,memory_max=args.memory_max)
            if result['events_created']: print(json.dumps(result,sort_keys=True),flush=True)
            stop.wait(args.interval)


if __name__ == '__main__':
    try: main()
    except Exception as error:
        print(json.dumps(dict(state='health_observer_failed',error_type=type(error).__name__)),flush=True)
        raise SystemExit(2) from None
