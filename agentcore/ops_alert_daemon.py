"""Opt-in polling loop for the durable capability-drift alert outbox.

``agentcore.ops_alerts`` owns the durable records, the lease-based claim, and
the acknowledgement that requires an explicit transport receipt;
``deliver_once`` performs exactly one claim/send/ack cycle. This module adds
only the loop a host process needs around that primitive:

* one delivery attempt per iteration, so a slow endpoint cannot stall the loop
  or hold more than one lease at a time;
* an ``idle_interval`` wait when the outbox has nothing claimable;
* exponential backoff (``backoff_initial`` .. ``backoff_max``) after any
  attempt that was not confirmed as delivered;
* prompt exit when a stop event is set, plus an optional iteration bound for
  tests and one-shot runs.

A failed attempt intentionally leaves the row pending but leased. Until that
lease expires ``claim`` returns ``None`` and the loop reports ``empty``, so a
failed event is never delivered twice inside its lease window. The outbox,
not this daemon, owns lease expiry and attempt accounting; the daemon only
paces itself.

The daemon creates no outbox, no transport, no background thread, and reads no
environment variable or configuration file. The host builds ``AlertOutbox``
and a ``Sender`` (see ``agentcore.ops_alert_senders``), owns process
supervision (systemd/cron), and decides where the queue database lives.
Unexpected exceptions -- including database errors -- propagate: a supervisor
should restart the process instead of the loop masking a broken store.

Default-off: nothing in this repository constructs or starts this daemon.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Any, Callable

from agentcore.ops_alerts import AlertOutbox, Sender, deliver_once

# ``deliver_once`` reports "unavailable", "delivery_unknown",
# "delivery_rejected", "ack_unknown" or "sent_ack_rejected" when transport
# acceptance was not confirmed. Every status that is neither "delivered" nor
# "empty" is treated as a retryable failure: the event stays pending in the
# outbox and the loop waits out the backoff before polling again.

_MAX_BACKOFF_EXPONENT = 62


@dataclass(frozen=True)
class DaemonStats:
    """Counters for one ``run`` call; ``last_status`` is the final outcome."""

    iterations: int = 0
    delivered: int = 0
    empty: int = 0
    failed: int = 0
    last_status: str | None = None


def _positive(name: str, value: float) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValueError(f"{name} must be a finite positive number")
    return float(value)


class AlertDeliveryDaemon:
    """Poll ``AlertOutbox`` and deliver pending events through ``sender``.

    ``lease`` is passed to ``AlertOutbox.claim``; ``idle_interval`` paces empty
    polls. ``sleep`` is an injection point for tests only: when it is omitted
    the loop waits on ``stop.wait(delay)``, which returns immediately on
    shutdown. A custom sleep is still bounded by stop checks before and after
    every call.
    """

    def __init__(self, outbox: AlertOutbox, sender: Sender, *,
                 lease: float = 60.0, idle_interval: float = 5.0,
                 backoff_initial: float = 1.0, backoff_max: float = 60.0,
                 sleep: Callable[[float], None] | None = None):
        if outbox is None:
            raise ValueError("outbox is required")
        if sender is None:
            raise ValueError("sender is required; the daemon never invents a transport")
        if sleep is not None and not callable(sleep):
            raise TypeError("sleep must be callable")
        self.outbox = outbox
        self.sender = sender
        self.lease = _positive("lease", lease)
        self.idle_interval = _positive("idle_interval", idle_interval)
        self.backoff_initial = _positive("backoff_initial", backoff_initial)
        self.backoff_max = _positive("backoff_max", backoff_max)
        self._sleep = sleep

    def run_once(self) -> dict[str, Any]:
        """Claim and attempt exactly one pending event."""
        return deliver_once(self.outbox, self.sender, lease=self.lease)

    def run(self, *, stop: threading.Event | None = None,
            max_iterations: int | None = None) -> DaemonStats:
        """Deliver until ``stop`` is set or ``max_iterations`` is reached.

        ``stop`` defaults to a fresh event that is never set, so a normal
        daemon call needs an explicit event to shut down. ``max_iterations``
        bounds the loop for tests and one-shot checks; it must be positive.
        """
        if max_iterations is not None and (
                isinstance(max_iterations, bool)
                or not isinstance(max_iterations, int) or max_iterations < 1):
            raise ValueError("max_iterations must be a positive integer or None")
        stop = stop if stop is not None else threading.Event()
        iterations = delivered = empty = failed = 0
        last_status: str | None = None
        consecutive_failures = 0
        while not stop.is_set():
            if max_iterations is not None and iterations >= max_iterations:
                break
            status = self.run_once().get("status")
            iterations += 1
            last_status = status
            if status == "delivered":
                delivered += 1
                consecutive_failures = 0
                delay = 0.0
            elif status == "empty":
                empty += 1
                consecutive_failures = 0
                delay = self.idle_interval
            else:
                failed += 1
                consecutive_failures += 1
                exponent = min(consecutive_failures - 1, _MAX_BACKOFF_EXPONENT)
                delay = min(self.backoff_max,
                            self.backoff_initial * (2 ** exponent))
            if max_iterations is not None and iterations >= max_iterations:
                break
            if stop.is_set():
                break
            self._wait(delay, stop)
        return DaemonStats(iterations=iterations, delivered=delivered,
                           empty=empty, failed=failed, last_status=last_status)

    def _wait(self, delay: float, stop: threading.Event) -> None:
        if delay <= 0:
            return
        if self._sleep is not None:
            self._sleep(delay)
            return
        stop.wait(delay)
