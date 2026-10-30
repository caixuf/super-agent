"""Offline regression suite for capability drift episode delivery."""
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import math

import pytest

from agentcore.ops_alerts import AlertOutbox, DeliveryReceipt, deliver_once
from agentcore.pool import Pool


REPO = Path(__file__).resolve().parents[1]
PLUGIN_FILE = REPO / "plugins/capability_alert_outbox/__init__.py"
spec = importlib.util.spec_from_file_location("capability_alert_outbox_plugin", PLUGIN_FILE)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


def drift(value=1):
    return {"drifted": True, "expected": {"capability": 8},
            "observed": {"capability": value}, "source": "synthetic-inspector"}


class FakeSender:
    def __init__(self):
        self.keys = set()
        self.calls = []
        self.fail = False

    def send(self, payload, *, idempotency_key):
        self.calls.append((payload, idempotency_key))
        if self.fail:
            raise ConnectionError("synthetic delivery failure")
        self.keys.add(idempotency_key)
        return DeliveryReceipt(True, idempotency_key, "receipt-" + idempotency_key[:12])


def observe(outbox, result=None):
    return outbox.observe("profile-test", "scope-test", result or drift(),
                          config_persisted=False, runtime_loaded=False)


def test_healthy_observation_is_silent_and_drift_event_has_repair_state(tmp_path):
    outbox = AlertOutbox(str(tmp_path / "queue"))
    quiet = observe(outbox, {"drifted": False, "expected": {"capability": 8},
                             "observed": {"capability": 8}})
    assert quiet == {"drifted": False, "closed_episode": False, "event_id": None, "created": False}
    assert outbox.pending() == []
    event = observe(outbox)
    assert event["created"] is True
    assert event["event"]["expected"] == {"capability": 8}
    assert event["event"]["observed"] == {"capability": 1}
    assert event["event"]["config_persisted"] is False
    assert event["event"]["runtime_loaded"] is False


def test_same_episode_deduplicates_then_health_allows_new_generation(tmp_path):
    outbox = AlertOutbox(str(tmp_path / "queue"))
    first = observe(outbox)
    repeated = observe(outbox, drift(2))
    assert repeated["created"] is False
    assert repeated["event_id"] == first["event_id"]
    recovered = observe(outbox, {"drifted": False, "expected": 8, "observed": 8})
    assert recovered["closed_episode"] is True
    second = observe(outbox)
    assert second["created"] is True
    assert second["event"]["generation"] == 2
    assert second["event_id"] != first["event_id"]
    assert len(outbox.pending()) == 2


def test_alert_rows_are_isolated_from_task_receipt_outbox(tmp_path):
    root = str(tmp_path / "queue")
    outbox = AlertOutbox(root)
    observe(outbox)
    pool = Pool(root)
    with pool.db() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM ops_alert_outbox").fetchone()[0] == 1


def test_new_store_instance_recovers_pending_alert_and_retries_failure(tmp_path):
    root = str(tmp_path / "queue")
    store = AlertOutbox(root)
    event_id = observe(store)["event_id"]
    restored = AlertOutbox(root)
    sender = FakeSender()
    sender.fail = True
    result = deliver_once(restored, sender, lease=60)
    assert result["status"] == "delivery_unknown"
    assert result["event_id"] == event_id
    assert len(restored.pending()) == 1
    # Simulate expiry without sleeping; the lease itself is durable and bounded.
    with restored.pool.db() as db:
        db.execute("UPDATE ops_alert_outbox SET lease_until=0 WHERE event_id=?", (event_id,))
    sender.fail = False
    delivered = deliver_once(restored, sender)
    assert delivered["status"] == "delivered"
    assert delivered["attempt"] == 2
    assert delivered["receipt_id"] == "receipt-" + event_id[:12]
    assert sender.calls[-1][1] == event_id
    assert restored.pending() == []
    with restored.pool.db() as db:
        assert db.execute("SELECT receipt_id FROM ops_alert_outbox WHERE event_id=?",
                          (event_id,)).fetchone()[0] == delivered["receipt_id"]


@pytest.mark.parametrize("lease", [0, -1, math.nan, math.inf, -math.inf, True, "1"])
def test_claim_rejects_nonpositive_nonfinite_or_non_numeric_lease(tmp_path, lease):
    outbox = AlertOutbox(str(tmp_path / "queue"))
    with pytest.raises(ValueError, match="finite positive"):
        outbox.claim(lease)


class FixedResponseSender:
    def __init__(self, response):
        self.response = response

    def send(self, payload, *, idempotency_key):
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


@pytest.mark.parametrize(
    ("response", "status"),
    [
        (False, "delivery_rejected"),
        (None, "delivery_unknown"),
        ({"accepted": False, "event_id": "unused", "receipt_id": "no"}, "delivery_rejected"),
        ({"success": False, "receipt_id": "no"}, "delivery_rejected"),
        ({"accepted": True, "event_id": "wrong", "receipt_id": "receipt"}, "delivery_unknown"),
        ({"accepted": True, "event_id": "unused", "receipt_id": ""}, "delivery_unknown"),
        (True, "delivery_unknown"),
    ],
)
def test_unconfirmed_or_rejected_sender_response_never_acks(tmp_path, response, status):
    outbox = AlertOutbox(str(tmp_path / "queue"))
    event_id = observe(outbox)["event_id"]
    result = deliver_once(outbox, FixedResponseSender(response))
    assert result["status"] == status
    pending = outbox.pending()
    assert len(pending) == 1 and pending[0]["event_id"] == event_id
    with outbox.pool.db() as db:
        row = db.execute("SELECT delivered,receipt_id FROM ops_alert_outbox WHERE event_id=?",
                         (event_id,)).fetchone()
    assert row["delivered"] is None
    assert row["receipt_id"] is None


def test_transport_exception_is_unknown_and_keeps_alert_pending(tmp_path):
    outbox = AlertOutbox(str(tmp_path / "queue"))
    event_id = observe(outbox)["event_id"]
    result = deliver_once(outbox, FixedResponseSender(TimeoutError("ambiguous timeout")))
    assert result["status"] == "delivery_unknown"
    assert result["event_id"] == event_id
    assert outbox.pending()[0]["event_id"] == event_id


def test_direct_ack_requires_matching_explicit_receipt(tmp_path):
    outbox = AlertOutbox(str(tmp_path / "queue"))
    event_id = observe(outbox)["event_id"]
    claim = outbox.claim()
    assert claim is not None
    assert outbox.acknowledge(event_id, claim["token"], None) is False
    assert outbox.acknowledge(event_id, claim["token"], {
        "accepted": True, "event_id": "wrong", "receipt_id": "receipt"}) is False
    assert outbox.pending()[0]["event_id"] == event_id
    assert outbox.acknowledge(event_id, claim["token"], DeliveryReceipt(
        True, event_id, "verified-receipt")) is True


def test_unconfigured_sender_is_explicit_and_does_not_claim_event(tmp_path):
    outbox = plugin.create_outbox(str(tmp_path / "queue"))
    event_id = observe(outbox)["event_id"]
    result = plugin.deliver_pending(outbox, None)
    assert result == {"status": "unavailable", "reason": "no transport configured"}
    pending = outbox.pending()
    assert len(pending) == 1
    assert pending[0]["event_id"] == event_id
    assert pending[0]["attempts"] == 0


def test_sender_idempotency_key_handles_send_then_crash_before_ack(tmp_path):
    outbox = AlertOutbox(str(tmp_path / "queue"))
    event_id = observe(outbox)["event_id"]

    class IdempotentFake(FakeSender):
        def __init__(self):
            super().__init__()
            self.unique_deliveries = set()

        def send(self, payload, *, idempotency_key):
            self.calls.append((payload, idempotency_key))
            self.unique_deliveries.add(idempotency_key)
            return DeliveryReceipt(True, idempotency_key, "receipt-" + idempotency_key[:12])

    sender = IdempotentFake()
    first = outbox.claim(lease=60)
    sender.send(first["event"], idempotency_key=event_id)
    # Model process death after external acceptance but before local ack.
    with outbox.pool.db() as db:
        db.execute("UPDATE ops_alert_outbox SET lease_until=0 WHERE event_id=?", (event_id,))
    assert deliver_once(outbox, sender)["status"] == "delivered"
    assert len(sender.calls) == 2  # retry is attempted
    assert sender.unique_deliveries == {event_id}  # destination dedupes the retry


def test_concurrent_senders_claim_one_event_and_old_token_cannot_ack(tmp_path):
    root = str(tmp_path / "queue")
    outbox_a = AlertOutbox(root)
    observe(outbox_a)
    outbox_b = AlertOutbox(root)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda outbox: outbox.claim(lease=0.05), (outbox_a, outbox_b)))
    claimed = [claim for claim in claims if claim]
    assert len(claimed) == 1
    expired = claimed[0]
    with outbox_a.pool.db() as db:
        db.execute("UPDATE ops_alert_outbox SET lease_until=0 WHERE event_id=?", (expired["event_id"],))
    renewed = outbox_b.claim(lease=60)
    assert renewed is not None
    assert renewed["token"] != expired["token"]
    assert outbox_a.acknowledge(expired["event_id"], expired["token"], DeliveryReceipt(
        True, expired["event_id"], "stale-receipt")) is False
    assert outbox_b.acknowledge(renewed["event_id"], renewed["token"], DeliveryReceipt(
        True, renewed["event_id"], "current-receipt")) is True
    assert outbox_b.pending() == []


def test_plugin_is_a_thin_existing_inspector_adapter(tmp_path):
    outbox = plugin.create_outbox(str(tmp_path / "queue"))
    result = plugin.record_inspection(outbox, profile="p", scope="s", inspection=drift(),
                                      config_persisted=True, runtime_loaded=False)
    assert result["created"]
    assert result["event"]["config_persisted"] is True
    assert result["event"]["runtime_loaded"] is False

