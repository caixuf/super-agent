"""Backfill durable completions to the conversation that persisted with the job.

``completion_delivery`` drains receipts in queue order and stops at the first result it
cannot deliver. That is correct for a transport that can address any origin, but a chat
transport needs the route recorded at submit time. A task admitted before routes were
persisted therefore sits at the head forever and blocks every later completion.

This module is the routing policy between the two:

* **Addressability comes from the stored origin only.** A receipt is deliverable when
  its job recorded a ``route`` (and, when a profile is supplied, that route belongs to
  this profile). Nothing here reads the currently active session — a completion must
  never land in whichever chat happens to be open.
* **Unaddressable receipts are skipped, not acknowledged.** They stay pending so an
  operator can inspect them, while later deliverable completions keep flowing.
* **Every drain starts from the database.** Restart, reconnect or a second process
  changes nothing: the outbox lease and the recorded receipt id remain the only state.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from agentcore.completion_delivery import (
    MAX_SUMMARY_CHARS, deliver_once, inspect_pending,
)
from agentcore.pool import Pool

ROUTE_FIELD = 'route'
#: Upper bound on how many head-of-line receipts one policy pass may examine. Kept well
#: below the oldest SQLite host-parameter limit because each parked id becomes one
#: ``job NOT IN (?,...)`` binding.
PARK_SCAN_LIMIT = 500


def routable(address: Any, *, profile: Optional[str] = None) -> bool:
    """Whether a persisted origin names a conversation this transport can address."""
    if not isinstance(address, dict):
        return False
    route = address.get(ROUTE_FIELD)
    if not isinstance(route, str) or not route.strip():
        return False
    if profile is not None and address.get('profile') != profile:
        return False
    return True


def pending_addresses(pool: Pool, *, limit: int = PARK_SCAN_LIMIT) -> list[tuple[str, Any]]:
    """Pending ``(job, origin)`` pairs in delivery order, without claiming anything."""
    if limit < 1:
        raise ValueError('limit must be positive')
    with pool.db() as db:
        rows = db.execute(
            'SELECT outbox.job AS job, jobs.origin AS origin '
            'FROM outbox LEFT JOIN jobs ON jobs.id = outbox.job '
            'WHERE outbox.acknowledged=0 ORDER BY outbox.rowid LIMIT ?', (limit,)).fetchall()
    addresses = []
    for row in rows:
        try:
            address = json.loads(row['origin']) if row['origin'] else None
        except (TypeError, ValueError):
            address = None
        addresses.append((row['job'], address))
    return addresses


def parked_jobs(pool: Pool, *, profile: Optional[str] = None,
                limit: int = PARK_SCAN_LIMIT) -> list[str]:
    """Pending receipts that this profile cannot address; left pending by design."""
    return [job for job, address in pending_addresses(pool, limit=limit)
            if not routable(address, profile=profile)]


def drain(pool: Pool, transport: Any, *, profile: Optional[str] = None,
          limit: int = 100, lease: float = 60,
          max_summary: int = MAX_SUMMARY_CHARS, not_before=None) -> dict[str, Any]:
    """Deliver routable completions once, stepping over unaddressable ones.

    Returns a bounded status record. A non-``delivered`` outcome stops the pass so a
    refused or unknown transport result is not retried in a tight loop; the next poll
    retries after the lease window.
    """
    if limit < 1:
        raise ValueError('limit must be positive')
    if transport is None:
        return {'status': 'unavailable', 'delivered': 0,
                'reason': 'no transport configured', 'outcomes': []}
    addresses = pending_addresses(pool)
    skip = tuple(job for job, address in addresses
                 if not routable(address, profile=profile))
    outcomes: list[dict[str, Any]] = []
    for _ in range(limit):
        outcome = deliver_once(pool, transport, lease=lease, limit=max_summary, skip=skip,
                               not_before=not_before, origin_filter=lambda address: routable(address, profile=profile))
        if outcome['status'] == 'empty':
            if not outcomes:
                outcomes.append(outcome)
            break
        outcomes.append(outcome)
        if outcome['status'] == 'delivered':
            continue
        if outcome['status'] in ('unowned', 'delivery_deferred') and isinstance(outcome.get('task_id'), str):
            # A changed origin or uncertain direct send stays pending, but must
            # not hold unrelated completions behind it for this pass.
            skip = skip + (outcome['task_id'],)
            continue
        break
    delivered = [row for row in outcomes if row['status'] == 'delivered']
    notable = [row for row in outcomes if row['status'] not in ('delivered', 'empty')]
    status = notable[-1]['status'] if notable else ('delivered' if delivered else 'empty')
    return {'status': status, 'delivered': len(delivered),
            'parked': list(skip), 'outcomes': outcomes}


def inspect(pool: Pool, *, profile: Optional[str] = None, limit: int = 20,
            max_summary: int = MAX_SUMMARY_CHARS) -> list[dict[str, Any]]:
    """Read-only operator view: what a drain would deliver and what stays parked."""
    rows = inspect_pending(pool, limit=limit, max_summary=max_summary)
    for row in rows:
        row['deliverable'] = routable(row.get('origin'), profile=profile)
    return rows


def main(argv=None) -> int:
    """``python3 -m agentcore.completion_backfill --root <queue> [--profile P]``."""
    import argparse
    import sys
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--root', required=True, help='queue root (pool.db parent)')
    parser.add_argument('--profile', default=None)
    parser.add_argument('--limit', type=int, default=20)
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    print(json.dumps(inspect(Pool(args.root), profile=args.profile, limit=args.limit),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
