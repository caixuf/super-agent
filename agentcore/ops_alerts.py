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
        self.pool.init_ops_alert_schema()

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

        return self.pool.observe_drift_episode(
            profile,
            scope,
            drifted=inspection["drifted"],
            event_id_fn=self._event_id,
            expected=inspection["expected"],
            observed=inspection["observed"],
            config_persisted=config_persisted,
            runtime_loaded=runtime_loaded,
        )

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
        return self.pool.observe_health_condition(
            profile,
            scope,
            healthy=healthy,
            expected=expected,
            observed=observed,
            fault_grace=fault_grace,
            recovery_grace=recovery_grace,
            now=now,
            max_events=max_events,
            retain_delivered=retain_delivered,
        )

    def claim(self, lease: float = 60) -> dict[str, Any] | None:
        if (isinstance(lease, bool) or not isinstance(lease, (int, float))
                or not math.isfinite(lease) or lease <= 0):
            raise ValueError("lease must be a finite positive number")
        return self.pool.claim_ops_alert(lease)

    def acknowledge(self, event_id: str, token: str, response: DeliveryReceipt | dict[str, Any]) -> bool:
        outcome, receipt_id = _receipt_status(response, event_id)
        if outcome != "accepted" or receipt_id is None:
            return False
        return self.pool.acknowledge_ops_alert(event_id, token, receipt_id)

    def pending(self) -> list[dict[str, Any]]:
        return self.pool.pending_ops_alerts()


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
