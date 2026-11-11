"""Deterministic tests for Bot Desktop human screen takeover awareness.

Covers:
1. ``agentcore.screen_lease`` parsing, capability-token hashing, fail-closed
   handling, and ``HERMES_HOME`` resolution precedence.
2. ``agentcore.pool`` schema migration, ``display_stage()`` mapping, zero-stat
   unconfigured fast path, execution-timeout pause during human takeover, and
   normal timeout enforcement when the agent holds the screen.
3. ``agentcore.liveness`` ``inspect()`` and ``sweep()`` behavior while human
   screen takeover is active vs released.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import threading
import time

import pytest

from agentcore import liveness
from agentcore.liveness import LivenessLedger, TaskLiveness
from agentcore.pool import Pool, display_stage
import agentcore.pool as pool_mod
from agentcore.screen_lease import (
    HUMAN_TAKEOVER_STAGE,
    is_human_takeover,
    lease_file_path,
    read_bot_desktop_lease,
    resolve_job_hermes_home,
)


class ManualClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.value = float(start)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


def _write_lease(hermes_home: Path, payload: object) -> Path:
    path = lease_file_path(hermes_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)
    return path


# ---------------------------------------------------------------------------
# 1. screen_lease contract tests
# ---------------------------------------------------------------------------


def test_missing_or_unset_lease_defaults_to_agent(tmp_path: Path) -> None:
    assert read_bot_desktop_lease(None).to_public_dict() == {
        "holder": "agent",
        "is_human_takeover": False,
        "viewer_hash": None,
        "since": 0.0,
        "reason": "",
        "epoch": 0,
        "lease_exists": False,
        "fail_closed": False,
    }
    assert read_bot_desktop_lease("   ").holder == "agent"
    assert is_human_takeover(None) is False

    missing_state = read_bot_desktop_lease(tmp_path / "profile-a")
    assert missing_state.holder == "agent"
    assert missing_state.is_human_takeover is False
    assert missing_state.lease_exists is False
    assert missing_state.fail_closed is False


def test_valid_human_lease_hashes_viewer_token_and_never_leaks_secret(tmp_path: Path) -> None:
    secret_token = "cap_tok_super_secret_viewer_987654321"
    expected_hash = hashlib.sha256(secret_token.encode("utf-8")).hexdigest()[:12]
    _write_lease(
        tmp_path,
        {
            "holder": "human",
            "viewer_id": secret_token,
            "since": 1700000123.5,
            "reason": "manual_2fa_entry",
            "epoch": 7,
        },
    )

    state = read_bot_desktop_lease(tmp_path)
    assert state.holder == "human"
    assert state.is_human_takeover is True
    assert state.viewer_hash == expected_hash
    assert len(state.viewer_hash) == 12
    assert state.since == 1700000123.5
    assert state.reason == "manual_2fa_entry"
    assert state.epoch == 7
    assert state.lease_exists is True
    assert state.fail_closed is False

    serialized = json.dumps(state.to_public_dict(), sort_keys=True)
    assert secret_token not in serialized
    assert secret_token not in repr(state)
    assert "viewer_id" not in state.to_public_dict()
    assert is_human_takeover(tmp_path) is True


def test_valid_agent_lease_reports_not_human_takeover(tmp_path: Path) -> None:
    _write_lease(
        tmp_path,
        {
            "holder": "agent",
            "viewer_id": "",
            "since": 1700000200.0,
            "reason": "released",
            "epoch": 8,
        },
    )
    state = read_bot_desktop_lease(tmp_path)
    assert state.holder == "agent"
    assert state.is_human_takeover is False
    assert state.viewer_hash is None
    assert state.lease_exists is True
    assert state.fail_closed is False
    assert is_human_takeover(tmp_path) is False


def test_corrupt_unreadable_or_invalid_lease_fails_closed_to_human(tmp_path: Path) -> None:
    # Case A: malformed JSON
    corrupt_home = tmp_path / "corrupt"
    corrupt_path = lease_file_path(corrupt_home)
    corrupt_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt_path.write_text("{not valid json", encoding="utf-8")
    corrupt_state = read_bot_desktop_lease(corrupt_home)
    assert corrupt_state.holder == "human"
    assert corrupt_state.is_human_takeover is True
    assert corrupt_state.lease_exists is True
    assert corrupt_state.fail_closed is True
    assert corrupt_state.reason == "lease_corrupt_json_fail_closed"

    # Case B: non-object JSON payload
    list_home = tmp_path / "non_object"
    _write_lease(list_home, ["human"])
    list_state = read_bot_desktop_lease(list_home)
    assert list_state.holder == "human"
    assert list_state.fail_closed is True
    assert list_state.reason == "lease_non_object_fail_closed"

    # Case C: unknown holder string
    bad_holder_home = tmp_path / "bad_holder"
    _write_lease(bad_holder_home, {"holder": "unknown_actor"})
    bad_holder_state = read_bot_desktop_lease(bad_holder_home)
    assert bad_holder_state.holder == "human"
    assert bad_holder_state.fail_closed is True
    assert bad_holder_state.reason == "lease_invalid_holder_fail_closed"

    # Case D: unreadable path (directory at lease.json triggers OSError even under root)
    unreadable_home = tmp_path / "unreadable"
    dir_lease = lease_file_path(unreadable_home)
    dir_lease.mkdir(parents=True, exist_ok=True)
    unreadable_state = read_bot_desktop_lease(unreadable_home)
    assert unreadable_state.holder == "human"
    assert unreadable_state.is_human_takeover is True
    assert unreadable_state.lease_exists is True
    assert unreadable_state.fail_closed is True
    assert unreadable_state.reason == "lease_unreadable_fail_closed"


def test_resolve_job_hermes_home_precedence(tmp_path: Path) -> None:
    p_hermes = str(tmp_path / "payload_hermes")
    p_profile = str(tmp_path / "payload_profile")
    e_hermes = str(tmp_path / "env_hermes")
    e_dev = str(tmp_path / "env_dev")

    env_both = {"HERMES_HOME": e_hermes, "DEV_PROFILE_HOME": e_dev}
    assert resolve_job_hermes_home(
        {"hermes_home": p_hermes, "profile_home": p_profile}, env_both
    ) == Path(p_hermes)
    assert resolve_job_hermes_home(
        {"hermes_home": "  ", "profile_home": p_profile}, env_both
    ) == Path(p_profile)
    assert resolve_job_hermes_home({}, env_both) == Path(e_hermes)
    assert resolve_job_hermes_home(
        None, {"HERMES_HOME": "", "DEV_PROFILE_HOME": e_dev}
    ) == Path(e_dev)
    assert resolve_job_hermes_home({}, {}) is None


# ---------------------------------------------------------------------------
# 2. Pool display_stage, migration, and execution-timeout pause tests
# ---------------------------------------------------------------------------


def test_display_stage_reports_human_takeover_wait_only_when_running_with_pid() -> None:
    assert (
        display_stage({"status": "running", "pid": 1234, "screen_takeover": 1})
        == HUMAN_TAKEOVER_STAGE
    )
    assert (
        display_stage({"status": "running", "pid": 1234, "screen_takeover": 0})
        == "running"
    )
    assert (
        display_stage({"status": "running", "pid": None, "screen_takeover": 1})
        == "starting"
    )
    assert (
        display_stage(
            {"status": "running", "pid": 1234, "screen_takeover": 1, "cancel": 1}
        )
        == "canceling"
    )


def test_pool_migrates_screen_takeover_column_on_existing_db(tmp_path: Path) -> None:
    pool_dir = tmp_path / "pool"
    pool = Pool(pool_dir)
    job = pool.submit("migrate-key", {"goal": "verify"})
    with pool.db() as db:
        db.execute("ALTER TABLE jobs DROP COLUMN screen_takeover")
    reopened = Pool(pool_dir)
    status = reopened.status(job)
    assert status is not None
    assert status["screen_takeover"] == 0
    assert status["stage"] == "queued"


def test_pool_unconfigured_job_performs_zero_lease_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("DEV_PROFILE_HOME", raising=False)

    calls: list[object] = []

    def spy_takeover(home: object) -> bool:
        calls.append(home)
        return False

    monkeypatch.setattr(pool_mod, "is_human_takeover", spy_takeover)

    pool = Pool(tmp_path / "pool")
    job = pool.submit("plain-job", {"goal": "no hermes home"})
    assert pool.run_once(
        [sys.executable, "-c", 'import json; print(json.dumps({"ok": True}))'],
        timeout=2.0,
        lease=2.0,
    )
    assert calls == []
    assert pool.status(job)["status"] == "completed"


def test_pool_run_claimed_pauses_timeout_during_human_takeover_and_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("DEV_PROFILE_HOME", raising=False)

    hermes_home = tmp_path / "hermes_profile"
    lease_path = _write_lease(
        hermes_home,
        {"holder": "human", "viewer_id": "tok_secret_123", "reason": "captcha", "epoch": 1},
    )

    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    liveness_db = state_root / "liveness.db"
    ledger = LivenessLedger(liveness_db)

    pool = Pool(state_root / "pool")
    job = pool.submit(
        "takeover-job",
        {"goal": "login_and_continue", "hermes_home": str(hermes_home)},
    )
    ledger.register(job, kind="durable", state="running", phase="executing")

    runner_script = tmp_path / "runner_takeover.py"
    runner_script.write_text(
        """import json
from pathlib import Path
import sys
import time

data = json.load(sys.stdin)
home = Path(data["payload"]["hermes_home"])
lease_file = home / "bot-desktop" / "lease.json"
while True:
    info = json.loads(lease_file.read_text(encoding="utf-8"))
    if info.get("holder") == "agent":
        break
    time.sleep(0.02)
time.sleep(0.08)
print(json.dumps({"ok": True, "result": "resumed_after_human"}))
""",
        encoding="utf-8",
    )

    claimed = pool.claim(lease=1.2)
    assert claimed is not None

    observed_stages: list[str] = []
    observed_deadlines: list[float] = []
    observed_liveness_phases: list[str] = []
    release_done = threading.Event()

    def human_operator() -> None:
        deadline_wait = time.monotonic() + 2.0
        while time.monotonic() < deadline_wait:
            st = pool.status(job)
            if st and st.get("pid") is not None and st.get("stage") == HUMAN_TAKEOVER_STAGE:
                observed_stages.append(st["stage"])
                if st.get("deadline") is not None:
                    observed_deadlines.append(float(st["deadline"]))
                break
            time.sleep(0.02)

        # Hold human takeover for ~0.9s, exceeding the 0.6s job execution timeout!
        hold_until = time.monotonic() + 0.9
        while time.monotonic() < hold_until:
            st = pool.status(job)
            if st:
                observed_stages.append(st["stage"])
                if st.get("deadline") is not None:
                    observed_deadlines.append(float(st["deadline"]))
            live_row = ledger.get(job)
            if live_row:
                observed_liveness_phases.append(live_row["phase"])
            time.sleep(0.08)

        _write_lease(
            hermes_home,
            {"holder": "agent", "viewer_id": "", "reason": "released", "epoch": 2},
        )
        assert lease_path.is_file()
        release_done.set()

    operator_thread = threading.Thread(target=human_operator, daemon=True)
    operator_thread.start()
    try:
        assert pool.run_claimed(
            claimed,
            [sys.executable, str(runner_script)],
            timeout=0.6,
            lease=0.6,
        )
    finally:
        operator_thread.join(timeout=3.0)

    assert release_done.is_set()
    assert HUMAN_TAKEOVER_STAGE in observed_stages
    assert len(observed_deadlines) >= 2
    assert max(observed_deadlines) > min(observed_deadlines)
    assert HUMAN_TAKEOVER_STAGE in observed_liveness_phases

    final_status = pool.status(job)
    assert final_status is not None
    assert final_status["status"] == "completed"
    assert final_status["stage"] == "completed"
    assert final_status["screen_takeover"] == 0

    receipt = pool.outbox_receipt(job)
    assert receipt is not None
    assert receipt["status"] == "completed"
    assert receipt["result"]["ok"] is True
    assert receipt["result"]["result"] == "resumed_after_human"
    assert receipt["result"]["paused_takeover_seconds"] >= 0.7


def test_pool_run_claimed_still_times_out_when_agent_holds_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("DEV_PROFILE_HOME", raising=False)

    hermes_home = tmp_path / "hermes_agent_mode"
    _write_lease(
        hermes_home,
        {"holder": "agent", "viewer_id": "", "reason": "autonomous", "epoch": 1},
    )

    pool = Pool(tmp_path / "pool")
    job = pool.submit(
        "agent-timeout-job",
        {"goal": "hang_without_human", "hermes_home": str(hermes_home)},
        max_tries=1,
    )
    claimed = pool.claim(lease=5.0)
    assert claimed is not None

    assert pool.run_claimed(
        claimed,
        [sys.executable, "-c", "import time; time.sleep(0.9)"],
        timeout=0.4,
        lease=5.0,
    )

    final_status = pool.status(job)
    assert final_status is not None
    assert final_status["status"] == "failed"
    assert final_status["screen_takeover"] == 0
    result = json.loads(final_status["result"])
    assert result["reason"] == "timeout"


# ---------------------------------------------------------------------------
# 3. Liveness inspect() and sweep() tests under human takeover
# ---------------------------------------------------------------------------


def test_liveness_inspect_reports_calm_note_and_not_quiet_during_human_takeover(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    store = TaskLiveness(tmp_path / "liveness.db", stall_seconds=60.0, clock=clock)
    assert TaskLiveness is LivenessLedger

    # Case 1: live PID silent past stall_seconds during human_takeover_wait
    store.register(
        "t_pid",
        kind="durable",
        state="running",
        pid=4321,
        pid_start="boot:99",
        phase=HUMAN_TAKEOVER_STAGE,
    )
    clock.advance(300.0)
    got_pid = store.inspect("t_pid", pid_alive=lambda pid, start: True)
    assert got_pid["verdict"] == "running"
    assert got_pid["stalled"] is False
    assert got_pid["quiet"] is False
    assert got_pid["liveness_note"] is not None
    assert HUMAN_TAKEOVER_STAGE in got_pid["liveness_note"]
    assert "不能确认任务仍有进展" not in got_pid["liveness_note"]

    # Case 2: non-PID task silent past stall_seconds during human_takeover_wait
    store.register(
        "t_nopid",
        kind="durable",
        state="running",
        phase=HUMAN_TAKEOVER_STAGE,
    )
    clock.advance(300.0)
    got_nopid = store.inspect("t_nopid")
    assert got_nopid["verdict"] == "running"
    assert got_nopid["stalled"] is False
    assert got_nopid["quiet"] is False
    assert got_nopid["liveness_note"] is not None
    assert HUMAN_TAKEOVER_STAGE in got_nopid["liveness_note"]
    assert "任务结果尚未确认" not in got_nopid["liveness_note"]


def test_liveness_sweep_preserves_running_under_human_takeover_phase_or_hermes_home(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    store = TaskLiveness(tmp_path / "liveness.db", stall_seconds=60.0, clock=clock)

    # Non-PID task explicitly in HUMAN_TAKEOVER_STAGE phase stays running past threshold
    store.register(
        "phase_wait",
        kind="durable",
        state="running",
        phase=HUMAN_TAKEOVER_STAGE,
    )
    store.register(
        "normal_silent",
        kind="durable",
        state="running",
        phase="executing",
    )
    clock.advance(180.0)
    changed = store.sweep()
    assert [row["task_id"] for row in changed] == ["normal_silent"]
    assert store.get("normal_silent")["state"] == "stalled"
    assert store.get("phase_wait")["state"] == "running"

    # When hermes_home has an active human lease, sweep preserves running AND restores stalled -> running
    hermes_home = tmp_path / "hermes_live"
    _write_lease(hermes_home, {"holder": "human", "viewer_id": "v1", "epoch": 1})
    restored = store.sweep(hermes_home=hermes_home)
    assert [row["task_id"] for row in restored] == ["normal_silent"]
    assert restored[0]["previous_state"] == "stalled"
    assert restored[0]["state"] == "running"
    assert store.get("normal_silent")["state"] == "running"

    # Once lease returns to agent and phase leaves takeover, silence marks stalled
    _write_lease(hermes_home, {"holder": "agent", "viewer_id": "", "epoch": 2})
    store.beat("phase_wait", phase="executing")
    clock.advance(180.0)
    stalled_after = store.sweep(hermes_home=hermes_home)
    assert {row["task_id"] for row in stalled_after} == {"phase_wait", "normal_silent"}
    assert store.get("phase_wait")["state"] == "stalled"
    assert store.get("normal_silent")["state"] == "stalled"


def test_liveness_dead_pid_still_becomes_lost_even_during_takeover_phase(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    store = TaskLiveness(tmp_path / "liveness.db", stall_seconds=60.0, clock=clock)
    store.register(
        "dead_pid_task",
        kind="durable",
        state="running",
        pid=9999,
        pid_start="boot:dead",
        phase=HUMAN_TAKEOVER_STAGE,
    )
    clock.advance(10.0)
    changed = store.sweep(pid_alive=lambda pid, start: False)
    assert len(changed) == 1
    assert changed[0]["state"] == "lost"
    inspected = store.inspect("dead_pid_task", pid_alive=lambda pid, start: False)
    assert inspected["verdict"] == "lost"
