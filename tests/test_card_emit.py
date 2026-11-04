"""End-to-end card flow through a fake transport: T5's finalize contract."""

from __future__ import annotations

import pytest

from agentcore.card_emit import CardEmitter, view_for
from agentcore.card_store import CardStore
from agentcore.task_card import CardHandle, CardFrame, CardState


class FakeTransport:
    """Records every view handed to it, in order. Stands in for a platform adapter."""

    def __init__(self):
        self.calls = []

    def emit(self, handle, view, *, final):
        self.calls.append((handle.message_id, view, final))

    @property
    def views(self):
        return [c[1] for c in self.calls]

    @property
    def message_ids(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def env(tmp_path):
    store = CardStore(tmp_path / "cards.db")
    store.bind("t1", "c1", "m1")
    transport = FakeTransport()
    ticks = iter(range(1000, 20000))
    emitter = CardEmitter(store, "t1", transport, title="跑用例", clock=lambda: next(ticks))
    return store, transport, emitter


def test_open_then_updates_all_go_to_one_message(env):
    _, transport, em = env
    em.open(phase="编译 matcher", host="node-a")
    em.update(phase="用例 8/14")
    em.finish(artifacts=("report.html",))
    # One task, one message id — every frame updates the same card.
    assert set(transport.message_ids) == {"m1"}
    assert [v.state for v in transport.views] == ["running", "running", "done"]


def test_final_view_is_last_and_marked_final(env):
    _, transport, em = env
    em.open(phase="build")
    em.finish(artifacts=("report.html",))
    last = transport.calls[-1]
    assert last[2] is True
    assert transport.views[-1].final is True
    assert transport.views[-1].artifacts == ("report.html",)


def test_late_update_after_finish_never_reaches_transport(env):
    _, transport, em = env
    em.open(phase="build")
    em.finish()
    before = len(transport.calls)
    assert em.update(phase="oops") is None
    assert len(transport.calls) == before  # the finished card is not touched again


def test_failure_is_terminal_and_offers_rerun_not_report(env):
    _, transport, em = env
    em.open(phase="build")
    em.fail(detail="runner exited 2")
    view = transport.views[-1]
    assert view.state == "failed"
    assert "rerun" in view.actions and "report" not in view.actions


def test_failure_is_never_rendered_as_done(env):
    _, transport, em = env
    em.fail(detail="boom")
    assert em.finish() is None
    assert all(v.state != "done" for v in transport.views)


def test_restart_resumes_the_same_card_and_sequence(tmp_path):
    store = CardStore(tmp_path / "cards.db")
    store.bind("t1", "c1", "m1")
    t1 = FakeTransport()
    CardEmitter(store, "t1", t1, clock=lambda: 5000.0).open(phase="build")

    # A new process: fresh store handle, fresh emitter, same card.
    store2 = CardStore(tmp_path / "cards.db")
    t2 = FakeTransport()
    em2 = CardEmitter(store2, "t1", t2, clock=lambda: 5000.0)
    assert em2.handle.message_id == "m1"
    em2.update(phase="cases")
    assert t2.message_ids == ["m1"]
    assert em2.handle.sequence == 2  # continued, never restarted at 1


def test_note_advances_while_quiet():
    h = CardHandle(task_id="t", chat_id="c", message_id="m", state=CardState.RUNNING,
                   started_at=100.0)
    early = view_for(h, CardFrame(1, CardState.RUNNING, "t:1"), elapsed=5)
    late = view_for(h, CardFrame(2, CardState.RUNNING, "t:2"), elapsed=185)
    assert early.note == "运行中 5秒"
    assert late.note == "运行中 3分05秒"


def test_view_render_is_platform_agnostic(env):
    _, transport, em = env
    em.open(phase="build", host="node-a")
    view = transport.views[-1].render()
    assert view["title"] == "跑用例"
    assert view["state"] == "running"
    assert view["actions"] == ["cancel", "pause", "status", "details", "logs"]
    assert view["final"] is False
