"""Proactive observation: it looks, it records, and in observe mode it stays quiet.

The tests pin the contract a reader depends on:
  * observe mode stores findings and sends nothing;
  * off mode does nothing at all;
  * a finding that repeats is stored once, not once per pass;
  * one broken watcher does not hide the others;
  * every finding names the action the agent *would* have taken.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentcore import proactive_watch as pw


class Clock:
    def __init__(self, start: float = 10_000.0):
        self.value = float(start)

    def __call__(self) -> float:
        return self.value


class FakeLedger:
    def __init__(self, rows):
        self.rows = rows

    def list_tasks(self, *, owner=None, active_only=True):
        return list(self.rows)


def _ledger_row(state, *, task_id="t1", label="跑测试套", minutes_ago=0, now=None):
    now = 10_000.0 if now is None else now
    ago = now - minutes_ago * 60
    return {"task_id": task_id, "state": state, "label": label,
            "updated_at": ago, "heartbeat_at": ago}


def test_observe_mode_records_but_delivers_nothing(tmp_path):
    now = 10_000.0
    ledger = FakeLedger([_ledger_row("unverified", minutes_ago=40, now=now)])
    store = pw.FindingStore(tmp_path / "p.db")

    result = pw.run_pass(store=store, ledger=ledger, mode="observe", now=now)

    assert result["ran"] is True
    assert result["stored"], "观察档应该记下发现"
    assert result["stored"][0]["watcher"] == "unverified"
    assert "提醒你验收" in result["stored"][0]["intent"]
    # 关键：没有任何发送字段被设置
    assert "sent" not in result and "delivered" not in result


def test_off_mode_does_nothing(tmp_path):
    now = 10_000.0
    store = pw.FindingStore(tmp_path / "p.db")
    ledger = FakeLedger([_ledger_row("unverified", minutes_ago=40, now=now)])

    result = pw.run_pass(store=store, ledger=ledger, mode="off", now=now)

    assert result == {"mode": "off", "ran": False, "stored": [],
                      "considered": 0, "errors": {}}
    assert store.recent() == []


def test_the_same_finding_is_not_recorded_twice(tmp_path):
    now = 10_000.0
    ledger = FakeLedger([_ledger_row("unverified", minutes_ago=40, now=now)])
    store = pw.FindingStore(tmp_path / "p.db")

    first = pw.run_pass(store=store, ledger=ledger, mode="observe", now=now)
    second = pw.run_pass(store=store, ledger=ledger, mode="observe", now=now)

    assert len(first["stored"]) == 1
    assert second["stored"] == [], "同一个发现不该重复刷屏"
    assert len(store.recent()) == 1


def test_fresh_tasks_are_not_reported(tmp_path):
    now = 10_000.0
    ledger = FakeLedger([_ledger_row("unverified", minutes_ago=1, now=now)])
    store = pw.FindingStore(tmp_path / "p.db")

    result = pw.run_pass(store=store, ledger=ledger, mode="observe", now=now)
    assert result["stored"] == []


def test_a_stalled_task_names_the_action_it_would_have_taken(tmp_path):
    now = 10_000.0
    ledger = FakeLedger([_ledger_row("stalled", minutes_ago=120, now=now)])
    store = pw.FindingStore(tmp_path / "p.db")

    stored = pw.run_pass(store=store, ledger=ledger, mode="observe", now=now)["stored"]

    assert stored and stored[0]["watcher"] == "stalled"
    assert "没有响应" in stored[0]["observation"]
    assert "清理账本" in stored[0]["intent"]


def test_one_broken_watcher_does_not_hide_the_others(tmp_path):
    now = 10_000.0
    store = pw.FindingStore(tmp_path / "p.db")

    def boom():
        raise RuntimeError("炸了")

    def fine():
        return [{"subject": "x", "observation": "观察", "intent": "打算做的事"}]

    result = pw.run_pass(store=store, mode="observe", now=now,
                         watchers={"boom": boom, "fine": fine})

    assert result["errors"]["boom"].startswith("RuntimeError")
    assert len(result["stored"]) == 1


def test_a_dirty_worktree_is_reported_with_its_age(tmp_path):
    import os

    now = 10_000.0
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "a.py"
    target.write_text("x", encoding="utf-8")
    os.utime(target, (now - 3 * 3600, now - 3 * 3600))   # last touched three hours ago

    class Done:
        returncode = 0
        stdout = " M a.py\n"

    findings = pw.watch_dirty_repo(repo, now=now, stale_seconds=3600,
                                   runner=lambda *a, **k: Done())

    assert findings and "未提交改动" in findings[0]["observation"]
    assert "180 分钟前" in findings[0]["observation"]
    assert "提交" in findings[0]["intent"]


def test_an_unknown_mode_is_refused(tmp_path):
    store = pw.FindingStore(tmp_path / "p.db")
    with pytest.raises(ValueError):
        pw.run_pass(store=store, mode="yolo")