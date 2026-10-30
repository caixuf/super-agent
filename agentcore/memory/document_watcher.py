"""Refresh a trusted curated source off the interactive conversation path.

The supervisor restarts this process; sync_document owns archival, publication,
source checks and the per-document process lock. Unchanged polls never open the
fact database. Failures remain retryable and logs contain metadata only.
"""
import argparse
import hashlib
import importlib
import json
from pathlib import Path
import signal
import threading
import time

from .config import get_config


class DocumentWatcher:
    def __init__(self, source, sync, *, settle_seconds=5, retry_seconds=30, clock=time.monotonic):
        self.source = Path(source)
        self.sync = sync
        self.clock = clock
        if settle_seconds < 0 or retry_seconds <= 0:
            raise ValueError('invalid watcher timing')
        self.settle_seconds = settle_seconds
        self.retry_seconds = retry_seconds
        self.observed = None
        self.observed_at = 0
        self.published = None
        self.retry_at = 0

    def revision(self):
        with self.source.open('rb') as stream:
            raw = stream.read(512*1024+1)
        if not raw or len(raw) > 512*1024:
            raise ValueError('curated_document_budget')
        return hashlib.sha256(raw).hexdigest()

    def poll(self):
        now = self.clock()
        try:
            revision = self.revision()
        except (OSError, ValueError) as exc:
            return {'state':'source_unavailable','error_type':type(exc).__name__}
        if revision != self.observed:
            self.observed = revision
            self.observed_at = now
            self.retry_at = 0
            return {'state':'settling','revision':revision}
        if revision == self.published:
            return {'state':'unchanged','revision':revision}
        if now-self.observed_at < self.settle_seconds or now < self.retry_at:
            return {'state':'waiting','revision':revision}
        try:
            result = self.sync(self.source, expected_revision=revision)
            if not isinstance(result,dict) or result.get('ok') is not True or result.get('revision') != revision:
                raise RuntimeError('curated_sync_incomplete')
            if self.revision() != revision:
                return {'state':'source_changed','revision':revision}
        except Exception as exc:
            # Never include exception bodies: providers and hooks can carry
            # private text, file contents or configuration in their messages.
            self.retry_at = self.clock()+self.retry_seconds
            return {'state':'retry','revision':revision,'error_type':type(exc).__name__}
        self.published = revision
        return {'state':'published','revision':revision}


def load_callable(spec):
    module,symbol=spec.split(':',1)
    return getattr(importlib.import_module(module),symbol)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bootstrap',required=True)
    parser.add_argument('--sync-hook',required=True,help='Trusted administrator module:callable')
    parser.add_argument('--source',required=True)
    parser.add_argument('--user',required=True)
    parser.add_argument('--agent',required=True)
    parser.add_argument('--interval',type=float,default=5)
    parser.add_argument('--retry-seconds',type=float,default=30)
    args=parser.parse_args()
    if not 1 <= args.interval <= 300 or not 1 <= args.retry_seconds <= 3600:
        parser.error('invalid watcher interval')
    load_callable(args.bootstrap)()
    cfg=get_config()
    if cfg.default_user != args.user or cfg.default_agent != args.agent:
        parser.error('watcher principal differs from configured memory principal')
    watcher=DocumentWatcher(args.source,load_callable(args.sync_hook),
                            settle_seconds=args.interval,retry_seconds=args.retry_seconds)
    stop=threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):
        signal.signal(sig,lambda *_:stop.set())
    previous=None
    while not stop.is_set():
        outcome=watcher.poll()
        if outcome != previous and outcome['state'] not in ('unchanged','waiting','settling'):
            print(json.dumps(outcome),flush=True)
        previous=outcome
        stop.wait(args.interval)


if __name__ == '__main__':
    main()
