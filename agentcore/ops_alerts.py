"""Durable, platform-neutral capability drift alert delivery.

The existing capability inspector remains the sole source of drift decisions.
This module stores episode state and alert delivery records in isolated tables
inside the shared Pool database; it does not modify task jobs or task receipts.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from agentcore.pool import Pool


class Sender(Protocol):
    def send(self, payload: dict[str, Any], *, idempotency_key: str) -> "DeliveryReceipt | dict[str, Any] | None": ...


@dataclass(frozen=True)
class DeliveryReceipt:
    """Explicit transport acceptance required before a durable ack."""

    accepted: bool
    event_id: str
    receipt_id: str


def _receipt_status(response: Any, expected_event_id: str) -> tuple[str, str | None]:
    """Validate explicit transport acceptance without trusting success prose."""
    if isinstance(response, DeliveryReceipt):
        accepted = response.accepted
        event_id = response.event_id
        receipt_id = response.receipt_id
    elif isinstance(response, dict):
        accepted = response.get("accepted")
        event_id = response.get("event_id")
        receipt_id = response.get("receipt_id")
        if response.get("success") is False:
            return "rejected", None
    else:
        if response is False:
            return "rejected", None
        return "unknown", None

    if accepted is False:
        return "rejected", None
    if (accepted is not True or event_id != expected_event_id
            or not isinstance(receipt_id, str) or not receipt_id.strip()):
        return "unknown", None
    return "accepted", receipt_id.strip()


class AlertOutbox:
    """Episode recorder and lease-fenced notification outbox."""

    def __init__(self, queue_root: str, *, pool: Pool | None = None):
        self.pool = pool or Pool(queue_root)
        with self.pool.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS ops_alert_episodes (
              profile TEXT NOT NULL,
              scope TEXT NOT NULL,
              generation INTEGER NOT NULL DEFAULT 0,
              active_event_id TEXT,
              updated REAL NOT NULL,
              PRIMARY KEY(profile, scope));
            CREATE TABLE IF NOT EXISTS ops_alert_outbox (
              event_id TEXT PRIMARY KEY,
              profile TEXT NOT NULL,
              scope TEXT NOT NULL,
              generation INTEGER NOT NULL,
              payload TEXT NOT NULL,
              created REAL NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0,
              lease_token TEXT,
              lease_until REAL,
              receipt_id TEXT,
              delivered REAL);
            CREATE INDEX IF NOT EXISTS ops_alert_pending_idx
              ON ops_alert_outbox(delivered, lease_until, created);
            CREATE TABLE IF NOT EXISTS ops_health_conditions (
              profile TEXT NOT NULL, scope TEXT NOT NULL,
              generation INTEGER NOT NULL DEFAULT 0,
              active_event_id TEXT, bad_since REAL, good_since REAL,
              last_at REAL NOT NULL, PRIMARY KEY(profile,scope));
            CREATE TABLE IF NOT EXISTS ops_health_events (
              event_id TEXT PRIMARY KEY);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(ops_alert_outbox)")}
            if "receipt_id" not in columns:
                db.execute("ALTER TABLE ops_alert_outbox ADD COLUMN receipt_id TEXT")

    @staticmethod
    def _event_id(profile: str, scope: str, generation: int) -> str:
        stable = json.dumps([profile, scope, generation], ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(stable.encode("utf-8")).hexdigest()

    def observe(self, profile: str, scope: str, inspection: dict[str, Any], *,
                config_persisted: bool, runtime_loaded: bool) -> dict[str, Any]:
        """Record one inspector observation; return the event only on a new drift.

        ``inspection`` is a structured result from the existing authoritative
        checker and must include ``drifted``, ``expected`` and ``observed``.
        This method never recalculates a baseline or repairs runtime state.
        """
        if not isinstance(profile, str) or not profile.strip():
            raise ValueError("profile is required")
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError("scope is required")
        if not isinstance(inspection, dict) or type(inspection.get("drifted")) is not bool:
            raise ValueError("inspector result must include boolean drifted")
        if "expected" not in inspection or "observed" not in inspection:
            raise ValueError("inspector result must include expected and observed")
        if type(config_persisted) is not bool or type(runtime_loaded) is not bool:
            raise ValueError("configuration and runtime state must be explicit booleans")

        now = time.time()
        with self.pool.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM ops_alert_episodes WHERE profile=? AND scope=?",
                             (profile, scope)).fetchone()
            if row is None:
                db.execute("INSERT INTO ops_alert_episodes(profile,scope,generation,updated) VALUES(?,?,0,?)",
                           (profile, scope, now))
                generation, active = 0, None
            else:
                generation, active = row["generation"], row["active_event_id"]

            if not inspection["drifted"]:
                db.execute("UPDATE ops_alert_episodes SET active_event_id=NULL,updated=? WHERE profile=? AND scope=?",
                           (now, profile, scope))
                return {"drifted": False, "closed_episode": active is not None,
                        "event_id": None, "created": False}
            if active:
                current = db.execute("SELECT payload FROM ops_alert_outbox WHERE event_id=?", (active,)).fetchone()
                return {"drifted": True, "closed_episode": False, "event_id": active,
                        "created": False, "event": json.loads(current["payload"]) if current else None}

            generation += 1
            event_id = self._event_id(profile, scope, generation)
            payload = {
                "kind": "capability_drift",
                "event_id": event_id,
                "profile": profile,
                "scope": scope,
                "generation": generation,
                "expected": inspection["expected"],
                "observed": inspection["observed"],
                "config_persisted": config_persisted,
                "runtime_loaded": runtime_loaded,
            }
            serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            db.execute("UPDATE ops_alert_episodes SET generation=?,active_event_id=?,updated=? WHERE profile=? AND scope=?",
                       (generation, event_id, now, profile, scope))
            db.execute("INSERT INTO ops_alert_outbox(event_id,profile,scope,generation,payload,created) VALUES(?,?,?,?,?,?)",
                       (event_id, profile, scope, generation, serialized, now))
            return {"drifted": True, "closed_episode": False, "event_id": event_id,
                    "created": True, "event": payload}

    def observe_health(self, profile: str, scope: str, *, healthy: bool,
                       expected: dict, observed: dict, fault_grace: float = 30,
                       recovery_grace: float = 10, now: float | None = None,
                       max_events: int = 4096, retain_delivered: int = 256) -> dict[str, Any]:
        """Debounce a condition durably and enqueue fault/recovery atomically.

        Repeated observations and observer restarts keep one fault episode.
        Pending events are never discarded; capacity failure rolls back both
        condition progress and event creation. Only this API's delivered history
        is eligible for trimming. The caller supplies sanitized observations.
        """
        if (not isinstance(profile,str) or not 1 <= len(profile) <= 128
                or not isinstance(scope,str) or not 1 <= len(scope) <= 128
                or type(healthy) is not bool or not isinstance(expected,dict)
                or not isinstance(observed,dict)):
            raise ValueError('invalid health observation')
        for value in (fault_grace,recovery_grace):
            if type(value) not in (int,float) or not math.isfinite(value) or not 0 <= value <= 3600:
                raise ValueError('invalid health grace')
        now = time.time() if now is None else now
        if type(now) not in (int,float) or not math.isfinite(now) or now < 0:
            raise ValueError('invalid observation time')
        if (type(max_events) is not int or not 2 <= max_events <= 4096
                or type(retain_delivered) is not int or not 0 <= retain_delivered < max_events):
            raise ValueError('invalid health retention budget')
        # Reject large/non-finite input even when debouncing produces no event.
        if len(json.dumps([expected,observed],allow_nan=False).encode()) > 8192:
            raise ValueError('health observation budget')
        with self.pool.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM ops_health_conditions WHERE profile=? AND scope=?',
                             (profile,scope)).fetchone()
            if row is None:
                if db.execute('SELECT count(*) FROM ops_health_conditions').fetchone()[0] >= 64:
                    raise RuntimeError('health_condition_capacity')
                generation,active,bad,good = 0,None,None,None
            else:
                generation,active,bad,good = (row[key] for key in
                    ('generation','active_event_id','bad_since','good_since'))
                if now < row['last_at']:
                    bad,good = None,None  # A clock regression cannot mature a grace window.
            phase = None
            if healthy:
                bad = None
                if active is not None:
                    good = now if good is None else good
                    if now-good >= recovery_grace: phase = 'recovery'
                else: good = None
            else:
                good = None
                bad = now if bad is None else bad
                if active is None and now-bad >= fault_grace: phase = 'fault'
            payload = None
            if phase is not None:
                old = db.execute('''SELECT e.event_id FROM ops_health_events e
                    JOIN ops_alert_outbox o ON o.event_id=e.event_id
                    WHERE o.delivered IS NOT NULL AND e.event_id NOT IN
                      (SELECT active_event_id FROM ops_health_conditions WHERE active_event_id IS NOT NULL)
                    ORDER BY o.delivered DESC,e.event_id DESC LIMIT -1 OFFSET ?''',
                    (retain_delivered,)).fetchall()
                ids = [(r[0],) for r in old]
                db.executemany('DELETE FROM ops_alert_outbox WHERE event_id=?',ids)
                db.executemany('DELETE FROM ops_health_events WHERE event_id=?',ids)
                if db.execute('SELECT count(*) FROM ops_alert_outbox').fetchone()[0] >= max_events:
                    raise RuntimeError('health_event_capacity')
                if phase == 'fault': generation += 1
                event_id = hashlib.sha256(json.dumps(
                    ['health',profile,scope,generation,phase],separators=(',',':')).encode()).hexdigest()
                payload = dict(kind='health_'+phase,event_id=event_id,profile=profile,scope=scope,
                    generation=generation,expected=expected,observed=observed,
                    fault_event_id=active if phase == 'recovery' else event_id)
                serialized = json.dumps(payload,ensure_ascii=False,sort_keys=True,allow_nan=False)
                db.execute('INSERT INTO ops_alert_outbox(event_id,profile,scope,generation,payload,created) VALUES(?,?,?,?,?,?)',
                           (event_id,profile,scope,generation,serialized,now))
                db.execute('INSERT INTO ops_health_events VALUES(?)',(event_id,))
                active = event_id if phase == 'fault' else None
                if phase == 'recovery': good = None
            db.execute('''INSERT INTO ops_health_conditions VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(profile,scope) DO UPDATE SET generation=excluded.generation,
                active_event_id=excluded.active_event_id,bad_since=excluded.bad_since,
                good_since=excluded.good_since,last_at=excluded.last_at''',
                (profile,scope,generation,active,bad,good,now))
            return dict(healthy=healthy,active_event_id=active,created=payload is not None,event=payload)

    def claim(self, lease: float = 60) -> dict[str, Any] | None:
        if (isinstance(lease, bool) or not isinstance(lease, (int, float))
                or not math.isfinite(lease) or lease <= 0):
            raise ValueError("lease must be a finite positive number")
        now = time.time()
        with self.pool.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""
                SELECT * FROM ops_alert_outbox
                WHERE delivered IS NULL AND (lease_until IS NULL OR lease_until < ?)
                ORDER BY created,event_id LIMIT 1
            """, (now,)).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute("UPDATE ops_alert_outbox SET lease_token=?,lease_until=?,attempts=attempts+1 WHERE event_id=?",
                       (token, now + lease, row["event_id"]))
            return {"event_id": row["event_id"], "token": token,
                    "lease_until": now + lease, "attempt": row["attempts"] + 1,
                    "event": json.loads(row["payload"])}

    def acknowledge(self, event_id: str, token: str, response: DeliveryReceipt | dict[str, Any]) -> bool:
        outcome, receipt_id = _receipt_status(response, event_id)
        if outcome != "accepted" or receipt_id is None:
            return False
        now = time.time()
        with self.pool.db() as db:
            updated = db.execute("""
                UPDATE ops_alert_outbox SET delivered=?,receipt_id=?,lease_token=NULL,lease_until=NULL
                WHERE event_id=? AND lease_token=? AND lease_until>? AND delivered IS NULL
            """, (now, receipt_id, event_id, token, now))
            return updated.rowcount == 1

    def pending(self) -> list[dict[str, Any]]:
        with self.pool.db() as db:
            rows = db.execute("SELECT event_id,payload,attempts,lease_until FROM ops_alert_outbox WHERE delivered IS NULL ORDER BY created,event_id").fetchall()
            return [{"event_id": r["event_id"], "event": json.loads(r["payload"]),
                     "attempts": r["attempts"], "lease_until": r["lease_until"]} for r in rows]


def deliver_once(outbox: AlertOutbox, sender: Sender | None, *, lease: float = 60) -> dict[str, Any]:
    """Deliver one alert with the stable event ID as the destination key.

    Ack requires ``DeliveryReceipt(accepted=True, event_id=<this event>,
    receipt_id=<nonempty>)`` or a dict with those same fields. A sender should
    implement idempotency for the event key. If it does not, a successful send
    followed by a crash before acknowledgement can be repeated.
    """
    if sender is None:
        return {"status": "unavailable", "reason": "no transport configured"}
    claim = outbox.claim(lease)
    if claim is None:
        return {"status": "empty"}
    try:
        response = sender.send(claim["event"], idempotency_key=claim["event_id"])
    except Exception as exc:
        # A transport exception may happen after remote acceptance. Keep the
        # item pending and classify it as unknown; retry may duplicate delivery.
        return {"status": "delivery_unknown", "event_id": claim["event_id"],
                "error_type": type(exc).__name__, "attempt": claim["attempt"]}
    outcome, receipt_id = _receipt_status(response, claim["event_id"])
    if outcome == "rejected":
        return {"status": "delivery_rejected", "event_id": claim["event_id"],
                "attempt": claim["attempt"]}
    if outcome != "accepted" or receipt_id is None:
        return {"status": "delivery_unknown", "event_id": claim["event_id"],
                "attempt": claim["attempt"]}
    try:
        acknowledged = outbox.acknowledge(claim["event_id"], claim["token"], response)
    except Exception as exc:
        # The database may have committed before an I/O error surfaced. A later
        # retry is safe only when the destination honors the event idempotency key.
        return {"status": "ack_unknown", "event_id": claim["event_id"],
                "error_type": type(exc).__name__, "attempt": claim["attempt"]}
    if not acknowledged:
        return {"status": "sent_ack_rejected", "event_id": claim["event_id"],
                "attempt": claim["attempt"]}
    return {"status": "delivered", "event_id": claim["event_id"],
            "receipt_id": receipt_id, "attempt": claim["attempt"]}
