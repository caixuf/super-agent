"""Offline regression suite for the alert delivery daemon loop.

No network, no production database: every case builds its own ``AlertOutbox``
under ``tmp_path``, injects a fake sender, and records the delays the loop asks
for. Only the default-wait case uses a short real thread, because that path is
``threading.Event.wait`` itself.
"""
import math
import threading
import time

import pytest

from agentcore.ops_alert_daemon import AlertDeliveryDaemon, DaemonStats
from agentcore.ops_alerts import AlertOutbox, DeliveryReceipt


def drift(value=1):
    return {"drifted": True, "expected": {"capability": 8},
            "observed": {"capability": value}, "source": "synthetic-inspector"}


def healthy(value=8):
    return {"drifted": False, "expected": {"capability": 8},
            "observed": {"capability": value}, "source": "synthetic-inspector"}


def observe(outbox, result=None):
    return outbox.observe("profile-test", "scope-test", result or drift(),
                          config_persisted=False, runtime_loaded=False)


def row(outbox, event_id):
    with outbox.pool.db() as db:
        return db.execute(
            "SELECT delivered,receipt_id,attempts FROM ops_alert_outbox WHERE event_id=?",
            (event_id,)).fetchone()


def expire_leases(outbox):
    """Pretend the claim lease elapsed so the next iteration can retry."""
    with outbox.pool.db() as db:
        db.execute("UPDATE ops_alert_outbox SET lease_until=0 WHERE delivered IS NULL")


class FakeSender:
    def __init__(self, response=None, *, fail=False):
        self.calls = []
        self.response = response
        self.fail = fail

    def send(self, payload, *, idempotency_key):
        self.calls.append((payload, idempotency_key))
        if self.fail:
            raise ConnectionError("synthetic delivery failure")
        if self.response is not None:
            if isinstance(self.response, BaseException):
                raise self.response
            return self.response
        return DeliveryReceipt(True, idempotency_key, "receipt-" + idempotency_key[:12])


class Responder(FakeSender):
    """Sender whose response is computed from the idempotency key."""

    def __init__(self, respond):
        super().__init__()
        self.respond = respond

    def send(self, payload, *, idempotency_key):
        self.calls.append((payload, idempotency_key))
        return self.respond(idempotency_key)


class RecordingSleep:
    def __init__(self, on_sleep=None):
        self.delays = []
        self.on_sleep = on_sleep

    def __call__(self, delay):
        self.delays.append(delay)
        if self.on_sleep is not None:
            self.on_sleep(delay)


def make_daemon(tmp_path, sender, **overrides):
    outbox = AlertOutbox(str(tmp_path / "queue"))
    return outbox, AlertDeliveryDaemon(outbox, sender, **overrides)


def test_pending_event_is_delivered_once_and_acked_with_receipt(tmp_path):
    sleep = RecordingSleep()
    outbox, daemon = make_daemon(tmp_path, FakeSender(), sleep=sleep)
    event_id = observe(outbox)["event_id"]

    stats = daemon.run(max_iterations=1)

    assert stats == DaemonStats(iterations=1, delivered=1, empty=0, failed=0,
                                last_status="delivered")
    assert [key for _, key in daemon.sender.calls] == [event_id]
    assert outbox.pending() == []
    stored = row(outbox, event_id)
    assert stored["delivered"] is not None
    assert stored["receipt_id"] == "receipt-" + event_id[:12]
    assert sleep.delays == []


def test_empty_poll_waits_idle_interval_instead_of_busy_spinning(tmp_path):
    sleep = RecordingSleep()
    outbox, daemon = make_daemon(tmp_path, FakeSender(), sleep=sleep)

    stats = daemon.run(max_iterations=3)

    assert stats == DaemonStats(iterations=3, delivered=0, empty=3, failed=0,
                                last_status="empty")
    assert daemon.sender.calls == []
    assert sleep.delays == [daemon.idle_interval, daemon.idle_interval]
    assert all(delay > 0 for delay in sleep.delays)


def test_retryable_failure_stays_pending_and_next_iteration_retries(tmp_path):
    outbox = AlertOutbox(str(tmp_path / "queue"))
    sender = FakeSender(fail=True)
    sleep = RecordingSleep(on_sleep=lambda _: expire_leases(outbox))
    daemon = AlertDeliveryDaemon(outbox, sender, sleep=sleep)
    event_id = observe(outbox)["event_id"]

    stats = daemon.run(max_iterations=2)

    assert stats.failed == 2
    assert stats.delivered == 0
    assert stats.last_status == "delivery_unknown"
    assert [key for _, key in sender.calls] == [event_id, event_id]
    pending = outbox.pending()
    assert len(pending) == 1 and pending[0]["event_id"] == event_id
    assert pending[0]["attempts"] == 2
    stored = row(outbox, event_id)
    assert stored["delivered"] is None and stored["receipt_id"] is None
    assert sleep.delays == [daemon.backoff_initial]


def test_failed_attempt_is_not_redelivered_inside_its_lease(tmp_path):
    outbox = AlertOutbox(str(tmp_path / "queue"))
    sender = FakeSender(fail=True)
    sleep = RecordingSleep()  # does not expire the lease
    daemon = AlertDeliveryDaemon(outbox, sender, sleep=sleep)
    observe(outbox)

    stats = daemon.run(max_iterations=2)

    assert stats.failed == 1
    assert stats.empty == 1
    assert len(sender.calls) == 1
    assert len(outbox.pending()) == 1


def test_backoff_grows_exponentially_and_is_capped(tmp_path):
    outbox = AlertOutbox(str(tmp_path / "queue"))
    sender = FakeSender(fail=True)
    sleep = RecordingSleep(on_sleep=lambda _: expire_leases(outbox))
    daemon = AlertDeliveryDaemon(outbox, sender, backoff_initial=1.0,
                                 backoff_max=5.0, sleep=sleep)
    observe(outbox)

    stats = daemon.run(max_iterations=5)

    assert stats.failed == 5
    assert sleep.delays == [1.0, 2.0, 4.0, 5.0]


def test_delivered_iterations_do_not_sleep_and_drain_one_event_each(tmp_path):
    outbox = AlertOutbox(str(tmp_path / "queue"))
    observe(outbox)
    outbox.observe("profile-test", "scope-test", healthy(),
                   config_persisted=False, runtime_loaded=False)
    second = observe(outbox)["event_id"]
    pending_ids = {item["event_id"] for item in outbox.pending()}
    assert len(pending_ids) == 2
    stop = threading.Event()
    sleep = RecordingSleep(on_sleep=lambda _: stop.set())
    sender = FakeSender()
    daemon = AlertDeliveryDaemon(outbox, sender, sleep=sleep)

    stats = daemon.run(stop=stop)

    assert stats.iterations == 3
    assert stats.delivered == 2
    assert stats.empty == 1
    assert stats.failed == 0
    assert {key for _, key in sender.calls} == pending_ids
    assert sleep.delays == [daemon.idle_interval]
    assert outbox.pending() == []
    assert row(outbox, second)["receipt_id"] == "receipt-" + second[:12]


@pytest.mark.parametrize(
    ("response", "expected_status"),
    [
        (lambda key: DeliveryReceipt(False, key, "receipt"), "delivery_rejected"),
        (lambda key: {"success": False, "receipt_id": "receipt"}, "delivery_rejected"),
        (lambda key: {"accepted": True, "event_id": key, "receipt_id": ""},
         "delivery_unknown"),
        (lambda key: {"accepted": True, "event_id": "other-event", "receipt_id": "rcpt"},
         "delivery_unknown"),
        (lambda key: None, "delivery_unknown"),
        (lambda key: True, "delivery_unknown"),
        (lambda key: TimeoutError("ambiguous timeout"), "delivery_unknown"),
    ],
)
def test_unconfirmed_sender_response_never_acks(tmp_path, response, expected_status):
    outbox = AlertOutbox(str(tmp_path / "queue"))
    event_id = observe(outbox)["event_id"]
    daemon = AlertDeliveryDaemon(outbox, Responder(response),
                                 sleep=RecordingSleep())

    stats = daemon.run(max_iterations=1)

    assert stats.failed == 1
    assert stats.last_status == expected_status
    assert outbox.pending()[0]["event_id"] == event_id
    stored = row(outbox, event_id)
    assert stored["delivered"] is None
    assert stored["receipt_id"] is None


def test_already_set_stop_event_exits_without_claiming(tmp_path):
    sleep = RecordingSleep()
    outbox, daemon = make_daemon(tmp_path, FakeSender(), sleep=sleep)
    observe(outbox)
    stop = threading.Event()
    stop.set()

    stats = daemon.run(stop=stop, max_iterations=10)

    assert stats == DaemonStats()
    assert daemon.sender.calls == []
    assert sleep.delays == []
    assert len(outbox.pending()) == 1


def test_stop_during_idle_wait_exits_gracefully(tmp_path):
    stop = threading.Event()
    sleep = RecordingSleep(on_sleep=lambda _: stop.set())
    outbox, daemon = make_daemon(tmp_path, FakeSender(), sleep=sleep)

    stats = daemon.run(stop=stop, max_iterations=10)

    assert stats == DaemonStats(iterations=1, empty=1, last_status="empty")
    assert sleep.delays == [daemon.idle_interval]


def test_stop_during_backoff_wait_exits_gracefully(tmp_path):
    stop = threading.Event()
    sleep = RecordingSleep(on_sleep=lambda _: stop.set())
    outbox = AlertOutbox(str(tmp_path / "queue"))
    daemon = AlertDeliveryDaemon(outbox, FakeSender(fail=True), sleep=sleep)
    observe(outbox)

    stats = daemon.run(stop=stop, max_iterations=10)

    assert stats.iterations == 1
    assert stats.failed == 1
    assert sleep.delays == [daemon.backoff_initial]


def test_default_wait_is_interrupted_by_stop_event(tmp_path):
    """The production path uses ``stop.wait`` and must not hold shutdown for 30s."""
    outbox, daemon = make_daemon(tmp_path, FakeSender(), idle_interval=30.0)
    stop = threading.Event()
    result = {}

    def runner():
        result["stats"] = daemon.run(stop=stop)

    thread = threading.Thread(target=runner)
    thread.start()
    time.sleep(0.1)
    assert thread.is_alive(), "daemon exited before entering its idle wait"
    started = time.monotonic()
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive(), "stop event did not interrupt the idle wait"
    assert time.monotonic() - started < 2
    stats = result["stats"]
    assert stats.iterations == 1
    assert stats.empty == 1
    assert stats.last_status == "empty"


def test_run_once_attempts_a_single_event(tmp_path):
    outbox, daemon = make_daemon(tmp_path, FakeSender())
    observe(outbox)

    first = daemon.run_once()
    second = daemon.run_once()

    assert first["status"] == "delivered"
    assert second == {"status": "empty"}


@pytest.mark.parametrize("max_iterations", [0, -1, True, 1.5, "2"])
def test_run_rejects_invalid_iteration_bound(tmp_path, max_iterations):
    _, daemon = make_daemon(tmp_path, FakeSender(), sleep=RecordingSleep())
    with pytest.raises(ValueError, match="positive integer"):
        daemon.run(max_iterations=max_iterations)


@pytest.mark.parametrize(
    "overrides",
    [
        {"lease": 0},
        {"lease": math.nan},
        {"lease": True},
        {"idle_interval": 0},
        {"idle_interval": -1},
        {"idle_interval": math.inf},
        {"backoff_initial": 0},
        {"backoff_max": "60"},
    ],
)
def test_constructor_rejects_invalid_timings(tmp_path, overrides):
    outbox = AlertOutbox(str(tmp_path / "queue"))
    with pytest.raises(ValueError, match="finite positive"):
        AlertDeliveryDaemon(outbox, FakeSender(), **overrides)


def test_constructor_requires_outbox_and_sender(tmp_path):
    outbox = AlertOutbox(str(tmp_path / "queue"))
    with pytest.raises(ValueError, match="outbox is required"):
        AlertDeliveryDaemon(None, FakeSender())
    with pytest.raises(ValueError, match="sender is required"):
        AlertDeliveryDaemon(outbox, None)
    with pytest.raises(TypeError, match="sleep must be callable"):
        AlertDeliveryDaemon(outbox, FakeSender(), sleep=1)
