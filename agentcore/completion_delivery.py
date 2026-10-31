"""Deliver durable task completions to the conversation that submitted them.

The queue already persists every terminal receipt (``pool.outbox``) and the submitting
conversation's address now persists with the job (``jobs.origin``). What was missing is
the consumer: nothing claimed those receipts, so a finished background task produced no
message at all — the user had to ask ``durable_status`` to learn the outcome.

Design rules, each with the failure it prevents:

* **Address from the persisted origin, never the current session.** A completion is
  delivered to the conversation that submitted the work, even after a reconnect, a
  window switch or a gateway restart. A task with no recorded origin is reported as
  ``unowned`` and left pending: guessing "the active conversation" is exactly how a
  private report lands in the wrong chat.
* **Ack only on explicit transport acceptance** (a receipt naming this task and a
  non-empty receipt id). Success prose, ``True``, ``None``, a bare dict or an exception
  never acknowledge, because none of them prove the message left the host.
* **At-least-once, never exactly-once.** The transport is given the task id as an
  idempotency key. If a send succeeds but the ack is lost, the next drain re-sends.
  This module does not claim to fix that; it reports ``ack_unknown`` honestly.
* **Read-only with respect to task state.** Delivery never rewrites ``jobs.status``;
  the queue remains the single authority for task outcome.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from agentcore.ops_alerts import DeliveryReceipt, _receipt_status
from agentcore.pool import Pool

#: How much of a task's report text one completion message carries. Long reports stay
#: on disk; the message says where to look instead of pasting megabytes into a chat.
MAX_SUMMARY_CHARS = 800

_TRUNCATION_MARK = '…(truncated; full report in the task result)'


class Transport(Protocol):
    """Host-supplied delivery. ``address`` is the persisted origin, ``message`` is neutral."""

    def deliver(self, address: dict[str, Any], message: dict[str, Any], *,
                idempotency_key: str) -> "DeliveryReceipt | dict[str, Any] | None": ...


#: Codex agent-turn-complete subset — map, do not vendor Rust notify.py.
AGENT_TURN_COMPLETE = "agent-turn-complete"

#: Origin keys mirrored onto the completion envelope when persisted (never guessed).
ORIGIN_ENVELOPE_KEYS = ("session", "chat", "thread", "turn", "route", "channel")


def origin_envelope_fields(origin: Any) -> dict[str, str]:
    """Copy addressable origin fields for the notify payload; blank-ok, never invent."""
    out: dict[str, str] = {}
    if not isinstance(origin, dict):
        return out
    for key in ORIGIN_ENVELOPE_KEYS:
        value = origin.get(key)
        if isinstance(value, str) and value.strip() and len(value.strip()) <= 512:
            out[key] = value.strip()
    return out


@dataclass(frozen=True)
class CompletionMessage:
    task_id: str
    status: str
    summary: str
    truncated: bool
    artifacts: tuple[str, ...] = ()
    colleague_role: str | None = None
    colleague_name: str | None = None
    colleague_label: str | None = None
    # Prefer-C2 turn-complete envelope (Codex subset map).
    notify_type: str = AGENT_TURN_COMPLETE
    session: str | None = None
    chat: str | None = None
    thread: str | None = None
    turn_id: str | None = None
    route: str | None = None
    channel: str | None = None
    role_id: str | None = None
    display_name: str | None = None

    def render(self) -> dict[str, Any]:
        result = {
            "type": self.notify_type or AGENT_TURN_COMPLETE,
            "task_id": self.task_id,
            "status": self.status,
            "summary": self.summary,
            "truncated": self.truncated,
        }
        if self.artifacts:
            result["artifacts"] = list(self.artifacts)
        if self.colleague_role:
            result["colleague_role"] = self.colleague_role
        if self.colleague_name:
            result["colleague_name"] = self.colleague_name
        if self.colleague_label:
            result["colleague_label"] = self.colleague_label
        if self.session:
            result["session"] = self.session
        if self.chat:
            result["chat"] = self.chat
        if self.thread:
            result["thread"] = self.thread
            result["thread_id"] = self.thread  # Codex-shaped alias
        if self.turn_id:
            result["turn_id"] = self.turn_id
        if self.route:
            result["route"] = self.route
        if self.channel:
            result["channel"] = self.channel
        if self.role_id:
            result["role_id"] = self.role_id
        if self.display_name:
            result["display_name"] = self.display_name
        return result


def _summary(result: Any, *, limit: int = MAX_SUMMARY_CHARS) -> tuple[str, bool]:
    """Extract a bounded, human-readable summary from a worker receipt result."""
    text = ''
    if isinstance(result, dict):
        for key in ('summary', 'report', 'message', 'reason'):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                break
        if not text:
            text = 'status: %s' % (result.get('reason') or result.get('status') or 'completed')
    elif isinstance(result, str) and result.strip():
        text = result.strip()
    if not text:
        text = 'completed'
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip() + _TRUNCATION_MARK, True


def build_message(receipt: dict[str, Any], *, limit: int = MAX_SUMMARY_CHARS,
                  colleague_role: str | None = None,
                  colleague_name: str | None = None,
                  colleague_label: str | None = None,
                  origin: Any = None,
                  role_id: str | None = None,
                  display_name: str | None = None) -> CompletionMessage:
    """Turn a queue receipt into the platform-neutral message a transport renders.

    Prefer-C2: envelope is Codex ``agent-turn-complete`` subset. Origin fields ride
    only when the caller passes the *persisted* jobs.origin — never an ambient chat.
    """
    if not isinstance(receipt, dict) or not isinstance(receipt.get('job'), str):
        raise ValueError('receipt must name its job')
    if colleague_role is not None and (not isinstance(colleague_role, str)
                                       or not 1 <= len(colleague_role) <= 64):
        raise ValueError('invalid colleague role')
    if colleague_name is not None and (not isinstance(colleague_name, str)
                                       or not 1 <= len(colleague_name) <= 80):
        raise ValueError('invalid colleague name')
    if colleague_label is not None and (not isinstance(colleague_label, str)
                                        or not 1 <= len(colleague_label) <= 80):
        raise ValueError('invalid colleague label')
    if role_id is not None and (not isinstance(role_id, str) or not 1 <= len(role_id) <= 64):
        raise ValueError('invalid role_id')
    if display_name is not None and (not isinstance(display_name, str)
                                     or not 1 <= len(display_name) <= 80):
        raise ValueError('invalid display_name')
    status = str(receipt.get('status') or 'unknown')
    result = receipt.get('result')
    # Worker protocol envelopes persist intact in the queue. Unwrap only an
    # explicit successful envelope, never arbitrary nested model fields.
    if isinstance(result, dict) and result.get('ok') is True and isinstance(result.get('result'), dict):
        result = result['result']
    summary, truncated = _summary(result, limit=limit)
    artifacts = result.get('artifacts', []) if isinstance(result, dict) else []
    if not isinstance(artifacts, list) or len(artifacts) > 8 or any(
            not isinstance(p, str) or not p or len(p) > 4096 for p in artifacts):
        raise ValueError('invalid completion artifacts')
    fields = origin_envelope_fields(origin)
    return CompletionMessage(
        task_id=receipt['job'], status=status, summary=summary,
        truncated=truncated, artifacts=tuple(artifacts),
        colleague_role=colleague_role, colleague_name=colleague_name,
        colleague_label=colleague_label,
        session=fields.get('session'), chat=fields.get('chat'),
        thread=fields.get('thread'), turn_id=fields.get('turn'),
        route=fields.get('route'), channel=fields.get('channel'),
        role_id=role_id or colleague_role,
        display_name=display_name or colleague_name,
    )


def _colleague_identity(pool: Pool, job: str) -> tuple[str | None, str | None, str | None]:
    """Host-persisted colleague binding for a completion; never parsed from model text."""
    try:
        with pool.db() as db:
            row = db.execute('SELECT payload FROM jobs WHERE id=?', (job,)).fetchone()
        payload = json.loads(row['payload']) if row and row['payload'] else {}
    except (TypeError, ValueError, KeyError):
        return None, None, None
    role = payload.get('_colleague_role')
    name = payload.get('_colleague_name')
    label = payload.get('_colleague_label')
    if not isinstance(role, str) or not 1 <= len(role) <= 64:
        return None, None, None
    if not isinstance(name, str) or not 1 <= len(name) <= 80:
        name = role
    if not isinstance(label, str) or not 1 <= len(label) <= 80:
        label = role
    return role, name, label


def deliver_once(pool: Pool, transport: Optional[Transport], *, lease: float = 60,
                 limit: int = MAX_SUMMARY_CHARS, skip=(), not_before=None,
                 origin_filter=None) -> dict[str, Any]:
    """Deliver at most one pending completion. Returns a status record, never raises.

    ``skip`` is forwarded to the ledger so a caller can leave a receipt it already knows
    it cannot address pending without letting that row block every later completion.
    """
    if transport is None:
        return {'status': 'unavailable', 'reason': 'no transport configured'}
    claim_options = {'not_before': not_before} if not_before is not None else {}
    if origin_filter is not None:
        claim_options['origin_filter'] = origin_filter
    claim = pool.claim_receipt(lease, skip=skip, **claim_options)
    if claim is None:
        return {'status': 'empty'}
    job, token = claim['receipt'].get('job'), claim['token']
    if not isinstance(job, str):
        # A malformed receipt cannot be addressed or acknowledged; leave it pending and say so.
        return {'status': 'invalid_receipt'}
    address = pool.origin(job)
    if not address or (origin_filter is not None and not origin_filter(address)):
        return {'status': 'unowned', 'task_id': job,
                'reason': 'task has no persisted origin; refusing to guess a conversation'}
    try:
        role, name, label = _colleague_identity(pool, job)
        message = build_message(
            claim['receipt'], limit=limit,
            colleague_role=role, colleague_name=name, colleague_label=label,
            origin=address,
        ).render()
    except (ValueError, TypeError):
        return {'status': 'invalid_receipt', 'task_id': job}
    try:
        response = transport.deliver(address, message, idempotency_key=job)
    except Exception as exc:
        # The transport may have delivered before raising. Stay pending, report unknown,
        # and accept that the retry can duplicate: at-least-once is the honest promise.
        return {'status': 'delivery_unknown', 'task_id': job,
                'error_type': type(exc).__name__}
    if (isinstance(response, dict) and response.get('accepted') is False
            and response.get('event_id') == job and response.get('deferred') is True):
        return {'status': 'delivery_deferred', 'task_id': job}
    outcome, receipt_id = _receipt_status(response, job)
    if outcome == 'rejected':
        return {'status': 'delivery_rejected', 'task_id': job}
    if outcome != 'accepted' or receipt_id is None:
        return {'status': 'delivery_unknown', 'task_id': job}
    try:
        acknowledged = pool.acknowledge(job, token, receipt_id=receipt_id)
    except Exception as exc:
        return {'status': 'ack_unknown', 'task_id': job, 'receipt_id': receipt_id,
                'error_type': type(exc).__name__}
    if not acknowledged:
        return {'status': 'sent_ack_rejected', 'task_id': job, 'receipt_id': receipt_id}
    return {'status': 'delivered', 'task_id': job, 'receipt_id': receipt_id}


def deliver_pending(pool: Pool, transport: Optional[Transport], *, limit: int = 100,
                    lease: float = 60, max_summary: int = MAX_SUMMARY_CHARS,
                    skip=()) -> list[dict[str, Any]]:
    """Drain pending completions once. Every non-progressing status stops the loop.

    Stopping (rather than spinning) matters: a rejected or unowned receipt stays pending
    by design, so an unconditional retry loop would rewrite the same row forever.
    """
    if limit < 1:
        raise ValueError('limit must be positive')
    outcomes = []
    for _ in range(limit):
        outcome = deliver_once(pool, transport, lease=lease, limit=max_summary, skip=skip)
        if outcome['status'] == 'empty':
            # Nothing left. An already-empty queue still reports one row so a caller can
            # tell "drained" from "never ran"; a trailing empty would just be noise.
            if not outcomes:
                outcomes.append(outcome)
            break
        outcomes.append(outcome)
        if outcome['status'] != 'delivered':
            break
    return outcomes


def inspect_pending(pool: Pool, *, limit: int = 20,
                    max_summary: int = MAX_SUMMARY_CHARS) -> list[dict[str, Any]]:
    """Read-only view of what a drain would deliver — the operator's dry run.

    Claims nothing and acknowledges nothing, so running it on a live queue cannot
    steal a completion from the real consumer.
    """
    if limit < 1:
        raise ValueError('limit must be positive')
    rows = []
    with pool.db() as db:
        pending = db.execute('SELECT job,receipt FROM outbox WHERE acknowledged=0 '
                             'ORDER BY rowid LIMIT ?', (limit,)).fetchall()
    for row in pending:
        try:
            message = build_message(json.loads(row['receipt']), limit=max_summary)
        except (ValueError, TypeError):
            rows.append({'task_id': row['job'], 'origin': pool.origin(row['job']),
                         'status': 'invalid_receipt'})
            continue
        rows.append({'task_id': row['job'], 'origin': pool.origin(row['job']),
                     'status': message.status, 'summary': message.summary,
                     'truncated': message.truncated})
    return rows



#: Terminal statuses that fire Prefer-C2 side-channel notify (pool outbox / host signal).
TERMINAL_NOTIFY_STATUSES = frozenset({"completed", "failed", "cancelled"})


def is_terminal_status(status: Any) -> bool:
    """Whether a pool / card status warrants a turn-complete notify."""
    if status is None:
        return False
    text = str(getattr(status, "value", status) or "").strip().lower()
    # CardState uses done; pool outbox uses completed — both terminal.
    if text == "done":
        text = "completed"
    return text in TERMINAL_NOTIFY_STATUSES


def kick_deliver_once(pool: Pool, transport: Optional[Transport], **kwargs) -> dict[str, Any]:
    """Prefer-C2 host-tick entry: ONE side-channel deliver attempt — never an LLM turn.

    Callers (worker finish hook, liveness terminal sink, host tick) invoke this after a
    durable terminal lands in the outbox. Idempotency remains ``job``; at-least-once
    honest. When ``transport`` is None the drain stays inspectable via
    :func:`inspect_pending` and returns ``unavailable`` — do not fake delivered.
    """
    return deliver_once(pool, transport, **kwargs)


def notify_on_terminal(pool: Pool, transport: Optional[Transport], *,
                       status: Any = None, **kwargs) -> dict[str, Any]:
    """Fire at most one notify when *status* is terminal (or status omitted = drain once).

    Explicit host turn-complete signal: pass ``status`` from the finish hook. Omitting
    status drains one pending receipt (host poll / tick). Never spawns a model turn.
    """
    if status is not None and not is_terminal_status(status):
        return {"status": "skipped", "reason": "non_terminal", "given": str(status)}
    return kick_deliver_once(pool, transport, **kwargs)


def main(argv=None) -> int:
    """``python3 -m agentcore.completion_delivery --root <queue>`` — dry-run listing.

    Delivery itself needs the host transport, so this CLI only inspects: it is how an
    operator confirms the address persisted with each pending completion *before*
    wiring a sender.
    """
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, help='queue root (pool.db parent)')
    parser.add_argument('--limit', type=int, default=20)
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    print(json.dumps(inspect_pending(Pool(args.root), limit=args.limit),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
