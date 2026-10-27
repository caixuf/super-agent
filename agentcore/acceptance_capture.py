"""Trusted-runtime capture for T1 acceptance evidence.

``agentcore.acceptance_evidence`` audits a capture it is handed; it cannot tell an
observed trace from an invented one. This module is the other half: the runtime that
actually sees inbound messages, turns, tool calls, task probes and transport receipts
records them *at the moment they happen*, on one host monotonic clock, and seals a
schema-2 capture that the auditor can then check.

It is deliberately not a log scraper. A monotonic timestamp cannot be recovered from
a wall-clock log afterwards, so reconstructing a capture post hoc would manufacture
exactly the property the audit is supposed to establish.

Failure-closed rules (all enforced here, not left to the auditor):

* an event type unknown to ``EVENT_SOURCES`` is refused at record time;
* ``source`` is derived from the type and may not be supplied by the caller;
* a duplicate event id, a non-finite timestamp, or an event outside the capture
  window is refused rather than sealed into an unsound capture;
* a capture that knows it lost events (a gap) seals with ``complete: False`` and the
  auditor therefore fails it — "unknown" is never reported as "nothing happened".
"""
from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path
from typing import Any, Callable

from agentcore.acceptance_evidence import EVENT_SOURCES, REQUIRED_SOURCES

SCHEMA_VERSION = 2
CLOCK = "host_monotonic"
CAPTURE_LINE = "__capture"
EXPECTATION_LINE = "__expectation"
GAP_LINE = "__gap"
SEALED_LINE = "__sealed"


def _finite(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


class Recorder:
    """Records one acceptance window. Thread-safe: the gateway mixes loop and worker threads.

    ``path`` appends every line to disk as it is recorded, so a crash mid-window still
    leaves the evidence that exists; such a file seals as incomplete (no ``__sealed``
    line), which is the honest answer.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic, path=None):
        self._clock = clock
        self._lock = threading.Lock()
        self._ids: set[str] = set()
        self._events: list[dict] = []
        self._requests: dict[str, dict] = {}
        self._gaps: list[str] = []
        self._sealed = False
        self._handle = None
        self.path = Path(path) if path is not None else None
        self.opened_at = self._now()
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open('a', encoding='utf-8')
            self._write({CAPTURE_LINE: True, 'opened_at': self.opened_at, 'clock': CLOCK,
                         'schema_version': SCHEMA_VERSION})

    # -- lifecycle ---------------------------------------------------------
    def _now(self) -> float:
        value = self._clock()
        if not _finite(value):
            raise ValueError('capture clock must return finite numbers')
        return value

    def _write(self, payload: dict) -> None:
        if self._handle is not None:
            self._handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + '\n')
            self._handle.flush()

    def gap(self, reason: str) -> None:
        """Declare that events were lost. The capture will not be sealed as complete."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('gap reason must be a nonempty string')
        with self._lock:
            self._refuse_when_sealed()
            self._gaps.append(reason)
            self._write({GAP_LINE: True, 'reason': reason})

    def seal(self, *, close_at: float | None = None) -> dict[str, Any]:
        """Freeze the window and return the schema-2 record the auditor consumes."""
        with self._lock:
            self._refuse_when_sealed()
            closed_at = self._now() if close_at is None else close_at
            if not _finite(closed_at) or closed_at < self.opened_at:
                raise ValueError('capture must close at or after it opened')
            for event in self._events:
                if event['at'] > closed_at:
                    raise ValueError('event recorded after the capture window closed')
            self._sealed = True
            self._write({SEALED_LINE: True, 'closed_at': closed_at,
                         'complete': not self._gaps})
            record = {
                'schema_version': SCHEMA_VERSION,
                'capture': {'opened_at': self.opened_at, 'closed_at': closed_at,
                            'complete': not self._gaps, 'clock': CLOCK,
                            'sources': sorted(REQUIRED_SOURCES)},
                'requests': json.loads(json.dumps(self._requests)),
                'events': json.loads(json.dumps(self._events)),
            }
            if self._handle is not None:
                self._handle.close()
                self._handle = None
            return record

    def _refuse_when_sealed(self) -> None:
        if self._sealed:
            raise RuntimeError('capture is sealed; start a new Recorder for a new window')

    # -- recording ---------------------------------------------------------
    def record(self, kind: str, event_id: str, *, at: float | None = None, **fields) -> dict:
        """Append one event. ``source`` follows from ``kind`` and is never caller-supplied."""
        if kind not in EVENT_SOURCES:
            raise ValueError('unknown event type: %r' % (kind,))
        if 'source' in fields:
            raise ValueError('event source is derived from its type, not supplied')
        if not isinstance(event_id, str) or not event_id:
            raise ValueError('event id must be a nonempty string')
        moment = self._now() if at is None else at
        if not _finite(moment):
            raise ValueError('event timestamp must be finite')
        with self._lock:
            self._refuse_when_sealed()
            if event_id in self._ids:
                raise ValueError('duplicate event id: %r' % (event_id,))
            if moment < self.opened_at:
                raise ValueError('event predates the capture window')
            event = {'type': kind, 'source': EVENT_SOURCES[kind], 'id': event_id, 'at': moment}
            event.update(fields)
            self._ids.add(event_id)
            self._events.append(event)
            self._write(dict(event))
            return event

    def expect(self, message_id: str, *, scenario: str, max_reply_seconds: float, **spec) -> dict:
        """Declare what this inbound message must be judged against."""
        if not isinstance(message_id, str) or not message_id:
            raise ValueError('expectation requires a message id')
        if not isinstance(scenario, str) or not scenario.strip():
            raise ValueError('expectation requires a scenario')
        if not _finite(max_reply_seconds) or max_reply_seconds <= 0:
            raise ValueError('latency budget must be a positive number of seconds')
        if 'scenario' in spec or 'max_reply_seconds' in spec:
            raise ValueError('scenario and budget are explicit arguments')
        with self._lock:
            self._refuse_when_sealed()
            if message_id in self._requests:
                raise ValueError('duplicate expectation for %r' % (message_id,))
            entry = {'scenario': scenario, 'max_reply_seconds': max_reply_seconds}
            entry.update(spec)
            self._requests[message_id] = entry
            self._write({EXPECTATION_LINE: True, 'message_id': message_id, **entry})
            return entry


def seal_from_file(path) -> dict[str, Any]:
    """Rebuild the record from a recorder file (crash forensics; no ``__sealed`` line = incomplete)."""
    source = Path(path)
    opened_at, closed_at, gaps, events, requests, sealed = None, None, [], [], {}, False
    ids = set()
    for line in source.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if item.get(CAPTURE_LINE):
            opened_at = item['opened_at']
        elif item.get(EXPECTATION_LINE):
            message_id = item.pop('message_id')
            item.pop(EXPECTATION_LINE)
            requests[message_id] = item
        elif item.get(GAP_LINE):
            gaps.append(item['reason'])
        elif item.get(SEALED_LINE):
            closed_at, sealed = item['closed_at'], True
        else:
            if item.get('id') in ids:
                raise ValueError('duplicate event id: %r' % (item.get('id'),))
            ids.add(item.get('id'))
            events.append(item)
    if opened_at is None:
        raise ValueError('capture file has no %s header' % CAPTURE_LINE)
    complete = sealed and not gaps
    if closed_at is None:  # never sealed: report the last observation, marked incomplete
        closed_at = max([opened_at] + [event['at'] for event in events])
    return {
        'schema_version': SCHEMA_VERSION,
        'capture': {'opened_at': opened_at, 'closed_at': closed_at,
                    'complete': complete, 'clock': CLOCK,
                    'sources': sorted(REQUIRED_SOURCES)},
        'requests': requests,
        'events': events,
    }


def main(argv=None) -> int:
    """``python3 -m agentcore.acceptance_capture <capture.ndjson>`` → schema-2 record on stdout."""
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        sys.stderr.write('usage: python3 -m agentcore.acceptance_capture <capture.ndjson>\n')
        return 2
    print(json.dumps(seal_from_file(args[0]), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
