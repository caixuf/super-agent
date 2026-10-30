"""Capability guard wiring: patrol result -> durable episode -> one message.

The guard must not invent a second capability baseline, must stay silent when
the inspector reports health, must send one message per drift episode, and must
never lose an alert because a sender is missing or a send failed.
"""
from __future__ import annotations

import copy

import pytest

from agentcore.capability_guard import format_alert, inspection_from_patrol, run_guard
from agentcore.ops_alerts import AlertOutbox, DeliveryReceipt


def healthy_patrol() -> dict:
    return {
        "ok": True,
        "autoheal": True,
        "baselines": {"agent.max_turns": 1000},
        "observed": [{"key": "agent.max_turns", "value": 1000, "floor": 1000}],
        "healed": [],
        "alerts": [],
        "note": "",
    }


def drift_patrol(observed: int = 15) -> dict:
    """The inspector repaired the floor in place; the repair needs a restart."""
    return {
        "ok": True,
        "autoheal": True,
        "baselines": {"agent.max_turns": 1000},
        "observed": [{"key": "agent.max_turns", "value": observed, "floor": 1000}],
        "healed": [{"key": "agent.max_turns", "from": observed, "to": 1000}],
        "note": "自愈只改白名单键，需重启网关生效",
    }


def unrepaired_patrol() -> dict:
    return {
        "ok": False,
        "autoheal": False,
        "baselines": {"agent.max_turns": 1000},
        "observed": [{"key": "agent.max_turns", "value": 15, "floor": 1000}],
        "healed": [],
        "alerts": ["agent.max_turns = 15 < 基线 1000（未开启自愈）"],
        "note": "",
    }


class FakeSender:
    """Fake transport: records attempts and returns an explicit receipt."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[tuple[dict, str]] = []

    def send(self, payload, *, idempotency_key):
        self.calls.append((payload, idempotency_key))
        if self.fail:
            raise ConnectionError("synthetic delivery failure")
        return DeliveryReceipt(True, idempotency_key, "receipt-" + idempotency_key[:12])


def guard(outbox, patrol, sender=None):
    return run_guard(outbox=outbox, patrol=patrol, profile="profile-test",
                     scope="scope-test", sender=sender)


def test_healthy_patrol_is_silent_and_closes_the_episode(tmp_path):
    outbox = AlertOutbox(str(tmp_path / "queue"))
    sender = FakeSender()

    quiet = guard(outbox, healthy_patrol(), sender)
    assert quiet["status"] == "quiet"
    assert quiet["episode"] == {"drifted": False, "closed_episode": False,
                                "event_id": None, "created": False}
    assert quiet["message"] is None
    assert quiet["pending_count"] == 0
    assert sender.calls == []

    delivered = guard(outbox, drift_patrol(), sender)
    assert delivered["status"] == "delivered"
    assert len(sender.calls) == 1

    recovered = guard(outbox, healthy_patrol(), sender)
    assert recovered["status"] == "quiet"
    assert recovered["episode"]["closed_episode"] is True
    assert len(sender.calls) == 1
    assert outbox.pending() == []


def test_new_drift_sends_once_and_repeated_drift_does_not_repeat(tmp_path):
    outbox = AlertOutbox(str(tmp_path / "queue"))
    sender = FakeSender()

    first = guard(outbox, drift_patrol(), sender)
    event_id = first["episode"]["event_id"]
    assert first["status"] == "delivered"
    assert first["episode"]["created"] is True
    assert first["episode"]["event"]["generation"] == 1
    assert first["pending_count"] == 0
    payload, key = sender.calls[0]
    assert key == event_id
    assert payload["event_id"] == event_id
    assert event_id in first["message"]

    repeated = guard(outbox, drift_patrol(observed=7), sender)
    assert repeated["status"] == "deduplicated"
    assert repeated["episode"]["created"] is False
    assert repeated["episode"]["event_id"] == event_id
    assert repeated["episode"]["event"]["generation"] == 1
    assert len(sender.calls) == 1
    assert outbox.pending() == []


def test_recovery_then_new_drift_gets_a_new_event_and_generation(tmp_path):
    outbox = AlertOutbox(str(tmp_path / "queue"))
    sender = FakeSender()

    first = guard(outbox, drift_patrol(), sender)
    guard(outbox, healthy_patrol(), sender)
    second = guard(outbox, drift_patrol(observed=3), sender)

    assert second["status"] == "delivered"
    assert second["episode"]["created"] is True
    assert second["episode"]["event"]["generation"] == 2
    assert second["episode"]["event_id"] != first["episode"]["event_id"]
    assert len(sender.calls) == 2
    assert outbox.pending() == []


def test_missing_sender_leaves_the_event_pending_and_retries_later(tmp_path):
    outbox = AlertOutbox(str(tmp_path / "queue"))

    result = guard(outbox, drift_patrol(), None)
    event_id = result["episode"]["event_id"]
    assert result["status"] == "pending"
    assert result["delivery"] == {"status": "unavailable",
                                  "reason": "no transport configured"}
    pending = outbox.pending()
    assert len(pending) == 1
    assert pending[0]["event_id"] == event_id
    assert pending[0]["attempts"] == 0

    sender = FakeSender()
    retried = guard(outbox, drift_patrol(), sender)
    assert retried["status"] == "delivered"
    assert retried["delivery"]["event_id"] == event_id
    assert [key for _, key in sender.calls] == [event_id]
    assert outbox.pending() == []


def test_failed_send_keeps_the_event_pending_and_retries_the_same_event(tmp_path):
    outbox = AlertOutbox(str(tmp_path / "queue"))
    sender = FakeSender(fail=True)

    failed = guard(outbox, drift_patrol(), sender)
    event_id = failed["episode"]["event_id"]
    assert failed["status"] == "pending"
    assert failed["delivery"]["status"] == "delivery_unknown"
    pending = outbox.pending()
    assert len(pending) == 1
    assert pending[0]["event_id"] == event_id
    assert pending[0]["attempts"] == 1

    # Lease expiry is durable; model it without sleeping so the retry is deterministic.
    with outbox.pool.db() as db:
        db.execute("UPDATE ops_alert_outbox SET lease_until=0 WHERE event_id=?", (event_id,))
    sender.fail = False
    retried = guard(outbox, drift_patrol(), sender)

    assert retried["status"] == "delivered"
    assert retried["delivery"]["event_id"] == event_id
    assert [key for _, key in sender.calls] == [event_id, event_id]
    assert outbox.pending() == []


def test_inspection_distinguishes_persisted_repair_from_effective_runtime():
    healed = inspection_from_patrol(drift_patrol())
    assert healed["drifted"] is True
    assert healed["config_persisted"] is True
    assert healed["runtime_loaded"] is False
    assert healed["expected"] == {"agent.max_turns": 1000}
    assert healed["observed"][0]["value"] == 15

    unrepaired = inspection_from_patrol(unrepaired_patrol())
    assert unrepaired["drifted"] is True
    assert unrepaired["config_persisted"] is False
    assert unrepaired["runtime_loaded"] is False

    healthy = inspection_from_patrol(healthy_patrol())
    assert healthy["drifted"] is False
    assert healthy["config_persisted"] is False
    assert healthy["runtime_loaded"] is True


def test_explicit_inspector_failure_is_never_treated_as_healthy():
    broken = {"ok": False, "baselines": {"agent.max_turns": 1000},
              "observed": [], "healed": [], "alerts": []}
    inspection = inspection_from_patrol(broken)
    assert inspection["drifted"] is True
    assert inspection["runtime_loaded"] is False


@pytest.mark.parametrize("bad", [None, [], "patrol", {"alerts": "boom"},
                                 {"baselines": ["agent.max_turns"]}])
def test_inspection_from_patrol_rejects_malformed_results(bad):
    with pytest.raises(ValueError):
        inspection_from_patrol(bad)


def test_format_alert_reports_values_and_does_not_claim_an_unloaded_repair(tmp_path):
    outbox = AlertOutbox(str(tmp_path / "queue"))
    result = guard(outbox, drift_patrol(), FakeSender())

    text = result["message"]
    assert "能力地板告警" in text
    assert "profile-test/scope-test" in text
    assert "第 1 次漂移" in text
    assert "agent.max_turns：实测 15，基线 1000" in text
    assert "配置已写入磁盘，重启网关后生效" in text
    assert "运行时：未确认加载基线" in text
    assert result["episode"]["event_id"] in text


def test_format_alert_marks_unconfigured_capability_and_manual_repair():
    event = {"kind": "capability_drift", "profile": "p", "scope": "s", "generation": 1,
             "event_id": "e" * 64, "expected": {"agent.max_turns": 1000},
             "observed": [{"key": "agent.max_turns", "value": None, "floor": 1000}],
             "config_persisted": False, "runtime_loaded": False}
    text = format_alert(copy.deepcopy(event))
    assert "agent.max_turns：未配置（基线 1000）" in text
    assert "配置未写入（需人工处理）" in text
    assert "运行时：未确认加载基线" in text


def test_format_alert_rejects_non_drift_payloads():
    with pytest.raises(ValueError):
        format_alert(None)
    with pytest.raises(ValueError):
        format_alert({"kind": "other"})
    with pytest.raises(ValueError):
        format_alert({"kind": "capability_drift", "expected": {}, "observed": {}})
