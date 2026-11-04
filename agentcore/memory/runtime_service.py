"""Supervised, single-principal memory ingestion using the canonical event queue."""
import argparse
import functools
import hashlib
import importlib
import json
import re
from pathlib import Path
import signal
import threading

from .config import get_config
from .event_worker import MemoryEventWorker
from .runtime_bridge import TurnEventQueue, enqueue_turn
from .turn_spool import TurnSpool, is_automated_turn
from . import memory_store, memory_trigger
from .memory_autonomy import AutonomousMemoryPipeline


def worker_observation(outcome,admitted=0):
    result=outcome.get('result') or {};key=outcome.get('idempotency_key')
    reason=result.get('reason');written=result.get('written')
    return dict(admitted=admitted,processed=outcome.get('processed',False),state=outcome.get('state'),
                lease_lost=outcome.get('lease_lost',False),
                event_sha256=hashlib.sha256(key.encode()).hexdigest() if isinstance(key,str) else None,
                reason=reason if isinstance(reason,str) and re.fullmatch('[a-z_]{1,64}',reason) else None,
                written=written if type(written) is int and written>=0 else None)


def run_and_flush(user_message, assistant_reply, **kwargs):
    """Compatibility entry for explicitly invoked extraction.

    The automatic queue path uses :class:`AutonomousMemoryPipeline`; this
    function remains for callers that intentionally invoke the legacy extractor
    direct-write API.
    """
    result = memory_trigger.run(user_message, assistant_reply, require_user_evidence=True, **kwargs)
    if result.get('ok') is True and memory_store.flush_background(timeout=30) != 0:
        return {'ok': False, 'retryable': True, 'reason': 'index_flush_incomplete'}
    return result


def scoped_runner(*, profile, user_id, agent_id, excluded_session_prefixes=(),
                  queue=None, model=None, receipt_resolver=None):
    if not isinstance(excluded_session_prefixes,(tuple,list)):
        raise ValueError('excluded session prefixes must be a sequence')
    prefixes=tuple(excluded_session_prefixes)
    if (len(prefixes)>32 or any(not isinstance(p,str) or not 1<=len(p)<=128 for p in prefixes)):
        raise ValueError('invalid excluded session prefixes')
    pipeline = AutonomousMemoryPipeline(queue=queue, model=model,
                                         receipt_resolver=receipt_resolver)
    def process(event):
        expected = dict(profile=profile, user_id=user_id, agent_id=agent_id, status='completed')
        if any(event.get(k) != v for k, v in expected.items()):
            return {'ok': False, 'retryable': False, 'reason': 'event_principal_mismatch'}
        if is_automated_turn(event.get('turn_id')):
            return {'ok': True, 'skipped': True, 'reason': 'automated_turn', 'written': 0}
        if any(str(event.get('session_id','')).startswith(prefix) for prefix in prefixes):
            return {'ok':True,'skipped':True,'reason':'excluded_session','written':0}
        return pipeline.process(event)
    return process


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bootstrap', required=True, help='Administrator module:callable that configures memory')
    p.add_argument('--spool', required=True)
    p.add_argument('--queue', required=True)
    p.add_argument('--profile', required=True)
    p.add_argument('--user', required=True)
    p.add_argument('--agent', required=True)
    p.add_argument('--interval', type=float, default=5)
    p.add_argument('--once', action='store_true')
    p.add_argument('--exclude-session-prefix',action='append',default=[])
    args = p.parse_args()
    if not 0.1 <= args.interval <= 300:
        p.error('interval must be between 0.1 and 300 seconds')
    mod, symbol = args.bootstrap.split(':', 1)
    getattr(importlib.import_module(mod), symbol)()
    cfg = get_config()
    if cfg.default_user != args.user or cfg.default_agent != args.agent:
        p.error('worker principal differs from configured memory principal')
    spool = TurnSpool(args.spool, profile=args.profile, user_id=args.user, agent_id=args.agent)
    queue = TurnEventQueue(Path(args.queue))
    worker = MemoryEventWorker(queue=queue, event_runner=scoped_runner(
        profile=args.profile,user_id=args.user,agent_id=args.agent,
        excluded_session_prefixes=args.exclude_session_prefix,queue=queue))
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        admitted = spool.ingest(functools.partial(enqueue_turn, queue=queue))
        outcome = worker.process_one()
        if admitted['enqueued'] or outcome.get('processed'):
            # No conversation text, extracted statements or exception bodies in logs.
            print(json.dumps(worker_observation(outcome,admitted['enqueued'])),flush=True)
        if args.once:
            break
        if not outcome.get('processed'):
            stop.wait(args.interval)
    if memory_store.flush_background(timeout=30) != 0:
        raise RuntimeError('memory_index_flush_incomplete')


if __name__ == '__main__':
    main()
