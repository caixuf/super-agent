"""Tests for the two-stage proactive hook engine (pre-hook probe -> durable dispatch)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentcore import proactive_hooks as ph


def _spec(tmp_path: Path, **overrides) -> ph.HookSpec:
    data = {
        "id": "nightly-build-watch",
        "role": "dev",
        "queue_root": str(tmp_path / "queue"),
        "probe_cmd": ["/bin/echo", "[SKIP]"],
        "goal_template": "Handle proactive event: {{summary}}\nDetails:\n{{details}}",
        "profile": "test-profile",
        "display_name": "Build Expert",
        "display_label": "Build",
        "origin": {
            "profile": "test-profile",
            "session": "proactive-hook:nightly-build-watch",
            "route": "chat:group-1",
            "channel": "chat",
            "chat": "group-1",
        },
        "cooldown_seconds": 3600.0,
        "timeout_seconds": 30.0,
    }
    data.update(overrides)
    return ph.HookSpec.from_dict(data)


def test_skip_probe_records_pass_and_dispatches_nothing(tmp_path: Path):
    spec = _spec(tmp_path)
    ledger = ph.HookLedger(tmp_path / "hooks.db")
    runner = lambda *a, **k: SimpleNamespace(returncode=0, stdout="[SKIP]\n", stderr="")

    res = ph.evaluate_and_dispatch(spec, ledger=ledger, runner=runner, now=1000.0)

    assert res == {"hook_id": "nightly-build-watch", "status": "skipped", "triggered": False}
    assert not (tmp_path / "queue" / "pool.db").exists()
    runs = ledger.recent_runs(hook_id="nightly-build-watch")
    assert len(runs) == 1 and runs[0]["status"] == "skipped"


def test_json_trigger_submits_durable_job_and_respects_cooldown(tmp_path: Path):
    spec = _spec(tmp_path)
    ledger = ph.HookLedger(tmp_path / "hooks.db")
    probe_out = json.dumps({
        "trigger": True,
        "dedupe_key": "commit-abc1234",
        "summary": "New branch commit abc1234 requires build",
        "details": "Branch main advanced to abc1234; no package found.",
    })
    runner = lambda *a, **k: SimpleNamespace(returncode=0, stdout=probe_out, stderr="")

    first = ph.evaluate_and_dispatch(spec, ledger=ledger, runner=runner, now=1000.0)
    assert first["status"] == "dispatched"
    assert first["triggered"] is True
    assert first["dedupe_key"] == "commit-abc1234"
    task_id = first["task_id"]
    assert len(task_id) == 32

    # Verify job in Pool carries bound origin and rendered goal
    with ph.Pool(spec.queue_root).db() as db:
        row = db.execute("SELECT payload, origin FROM jobs WHERE id=?", (task_id,)).fetchone()
    assert row is not None
    payload = json.loads(row["payload"])
    origin = json.loads(row["origin"])
    assert "New branch commit abc1234 requires build" in payload["goal"]
    assert payload["_colleague_role"] == "dev"
    assert payload["_proactive_hook_id"] == "nightly-build-watch"
    assert origin["route"] == "chat:group-1"

    # Second run within cooldown must be suppressed
    second = ph.evaluate_and_dispatch(spec, ledger=ledger, runner=runner, now=1500.0)
    assert second["status"] == "cooldown"
    assert second["triggered"] is False
    assert second["prior_task_id"] == task_id

    # Run after cooldown expires triggers again
    third = ph.evaluate_and_dispatch(spec, ledger=ledger, runner=runner, now=5000.0)
    assert third["status"] == "dispatched"
    assert third["triggered"] is True


def test_line_prefixed_trigger_and_dry_run(tmp_path: Path):
    spec = _spec(tmp_path)
    ledger = ph.HookLedger(tmp_path / "hooks.db")
    stdout = "[TRIGGER:req-9001] New requirement 9001\nNeeds test specification and case draft"
    runner = lambda *a, **k: SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    res = ph.evaluate_and_dispatch(spec, ledger=ledger, runner=runner, dry_run=True, now=1000.0)
    assert res["status"] == "dry_run_triggered"
    assert res["dedupe_key"] == "req-9001"
    assert "New requirement 9001" in res["goal"]
    assert not (tmp_path / "queue" / "pool.db").exists()


def test_nonzero_probe_exit_is_recorded_without_dispatching(tmp_path: Path):
    spec = _spec(tmp_path)
    ledger = ph.HookLedger(tmp_path / "hooks.db")
    runner = lambda *a, **k: SimpleNamespace(returncode=2, stdout="", stderr="network timeout")

    res = ph.evaluate_and_dispatch(spec, ledger=ledger, runner=runner, now=1000.0)
    assert res["status"] == "probe_failed"
    assert res["triggered"] is False
    assert "probe_exit_2" in res["error"]
