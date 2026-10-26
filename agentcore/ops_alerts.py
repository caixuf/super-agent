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
