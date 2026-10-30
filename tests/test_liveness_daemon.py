"""Daemon: transitions are emitted once, adapter failures stay isolated."""
from __future__ import annotations

import json
import threading

from agentcore.liveness import LivenessLedger
from agentcore.liveness_daemon import JsonlSink, LivenessDaemon


class Clock:
    def __init__(self, start: float = 1000.0):
        self.value = float(start)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _ledger(tmp_path, clock):
    return LivenessLedger(tmp_path / "liveness.db", stall_seconds=60, clock=clock)


def test_transition_emitted_once(tmp_path):
    clock = Clock()
    ledger = _ledger(tmp_path, clock)
    events = []
    daemon = LivenessDaemon(ledger, events.append, clock=clock)
    ledger.register("t1", kind="durable", state="running", label="suite")
    clock.advance(61)
    first = daemon.run_once()
    assert len(first) == 1
    assert first[0]["state"] == "stalled"
    assert first[0]["previous_state"] == "running"
    assert first[0]["task_id"] == "t1"
    assert first[0]["heartbeat_age_s"] >= 61
    assert daemon.run_once() == []
    assert len(events) == 1


def test_sink_failure_does_not_kill_the_loop(tmp_path):
    clock = Clock()
    ledger = _ledger(tmp_path, clock)
    daemon = LivenessDaemon(ledger, lambda event: (_ for _ in ()).throw(RuntimeError("boom")),
                            clock=clock)
    ledger.register("t1", kind="durable", state="running")
    clock.advance(61)
    assert daemon.run_once() == []
    assert daemon.errors == 1


def test_run_is_bounded_and_stop_is_honored(tmp_path):
    clock = Clock()
    ledger = _ledger(tmp_path, clock)
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds)

    daemon = LivenessDaemon(ledger, lambda event: None, clock=clock, sleep=fake_sleep,
                            interval=5)
    assert daemon.run(max_iterations=3) == {"iterations": 3, "errors": 0}
    assert sleeps == [5, 5]

    stop = threading.Event()
    stop.set()
    assert daemon.run(stop=stop)["iterations"] == 0


def test_jsonl_sink_roundtrip(tmp_path):
    path = tmp_path / "events.jsonl"
    sink = JsonlSink(path)
    sink({"task_id": "t1", "state": "stalled"})
    sink({"task_id": "t1", "state": "running"})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["state"] for line in lines] == ["stalled", "running"]
