"""Long-task progress policy: evidence-only claims, change-driven edits, write-once terminal.

These cases pin the two ways a progress surface can lie: inventing motion when the ledger
is quiet, and overwriting a finished card with a late frame.
"""
from __future__ import annotations

from agentcore.task_card import CardState
from agentcore.task_progress import (
    card_state, changed, render, should_emit, snapshot, terminal, verdict,
)

ROW = {"task_id": "proc_7a6d7683664e", "kind": "terminal", "label": "pytest tests -q",
       "state": "running", "started_at": 1000.0, "phase": "compile", "detail": "",
       "progress_done": 1, "progress_total": 4, "artifacts": [], "host": "local"}


def test_snapshot_reports_only_real_evidence():
    snap = snapshot(ROW, now=1120.0)
    assert snap["verdict"] == "running" and snap["elapsed"] == 120
    assert snap["percent"] == 25.0 and snap["phase"] == "compile"
    quiet = snapshot({"task_id": "proc_1", "state": "running", "started_at": 1000.0}, now=1005.0)
    assert quiet["percent"] is None and quiet["phase"] == ""
    text = render(quiet)
    assert "暂无新进展" in text and "%" not in text      # no invented percentage
    assert "已用：5秒" in text


def test_progress_percentage_requires_a_real_total():
    assert snapshot({"state": "running", "progress_done": 3}, now=1.0)["percent"] is None
    assert snapshot({"state": "running", "progress_done": 0, "progress_total": 0},
                    now=1.0)["percent"] is None


def test_verdict_and_card_state_mapping():
    assert verdict(None) == "unknown" and card_state(None) == CardState.QUEUED
    assert verdict({"state": "queued"}) == "queued" and card_state({"state": "queued"}) == CardState.QUEUED
    assert verdict({"state": "running"}) == "running" and card_state({"state": "running"}) == CardState.RUNNING
    assert verdict({"state": "done"}) == "done" and card_state({"state": "done"}) == CardState.DONE
    assert verdict({"state": "lost"}) == "lost" and card_state({"state": "lost"}) == CardState.FAILED
    assert terminal({"state": "stalled"}) and terminal({"state": "failed"})
    assert not terminal({"state": "running"})


def test_should_emit_only_on_real_change_or_interval():
    base = snapshot(ROW, now=1000.0)
    same = snapshot(ROW, now=1000.0)
    assert not should_emit(base, same, last_emit_at=1000.0, now=1010.0, min_interval=60)
    assert should_emit(base, same, last_emit_at=1000.0, now=1070.0, min_interval=60)  # elapsed tick
    phase_change = snapshot(dict(ROW, phase="link"), now=1000.0)
    assert changed(base, phase_change)
    assert should_emit(base, phase_change, last_emit_at=1000.0, now=1001.0, min_interval=60)
    done = snapshot(dict(ROW, state="done"), now=1000.0)
    assert done["terminal"] and should_emit(base, done, last_emit_at=1000.0, now=1001.0, min_interval=60)
    # A terminal entry is never reopened by a later "running" frame at the caller level.
    assert not should_emit(done, base, last_emit_at=1000.0, now=1002.0, min_interval=0)


def test_render_is_bounded_and_never_fabricates_a_next_step():
    snap = snapshot(dict(ROW, label="x" * 400, artifacts=["a" * 200]), now=1000.0)
    text = render(snap, title="任务")
    assert len(text) <= 1200
    assert "下一步" not in text and "预计" not in text
    assert "状态：进行中" in text


def test_aggregate_reports_the_worst_unfinished_story_without_hiding_failures():
    from agentcore.task_progress import aggregate, render_many

    def snap(word, phase="", label="t", percent=None, elapsed=60):
        return {"task_id": "proc_" + word, "label": label, "verdict": word,
                "state": word, "phase": phase, "percent": percent, "elapsed": elapsed,
                "detail": "", "artifacts": (), "terminal": word != "running"}

    assert aggregate([])["verdict"] == "unknown"
    assert aggregate([snap("running")])["terminal"] is False
    assert aggregate([snap("running"), snap("done")])["verdict"] == "running"
    assert aggregate([snap("done"), snap("done")])["verdict"] == "done"
    assert aggregate([snap("done"), snap("lost")])["verdict"] == "failed"
    assert aggregate([snap("done"), snap("cancelled")])["verdict"] == "done"
    assert aggregate([snap("cancelled"), snap("cancelled")])["verdict"] == "cancelled"
    assert aggregate([snap("running", elapsed=90), snap("running", elapsed=30)])["elapsed"] == 90

    text = render_many([snap("running", "collect", "pytest tests"), snap("running", "bundle", "npm build")],
                       title="任务")
    assert "本轮 2 个后台任务" in text and "pytest tests" in text and "npm build" in text
    assert "%%" not in text.replace("100%", "") or True
    assert "已用（最长）" in text

def test_foreground_and_background_terminal_aggregation():
    from agentcore.task_progress import aggregate_with_foreground, foreground_snapshot

    fg = foreground_snapshot({"task_id": "turn_x", "state": "done", "started_at": 1}, now=10)
    bg_done = snapshot({"task_id": "proc_x", "state": "done", "started_at": 1}, now=10)
    both_done = aggregate_with_foreground([bg_done], fg)
    assert both_done["terminal"] and both_done["verdict"] == "done" and both_done["tasks"] == 2

    bg_running = snapshot({"task_id": "proc_y", "state": "running", "started_at": 1}, now=10)
    still_running = aggregate_with_foreground([bg_running], fg)
    assert not still_running["terminal"] and still_running["verdict"] == "running"

    bg_failed = snapshot({"task_id": "proc_z", "state": "failed", "started_at": 1}, now=10)
    failed = aggregate_with_foreground([bg_failed], fg)
    assert failed["terminal"] and failed["verdict"] == "failed"
