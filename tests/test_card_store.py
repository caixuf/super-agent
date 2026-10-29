"""Durable card handles: binding, persistence and restart-resume."""

from __future__ import annotations

import pytest

from agentcore.card_store import CardStore
from agentcore.task_card import CardState


@pytest.fixture
def store(tmp_path):
    return CardStore(tmp_path / "cards.db")


def test_bind_is_idempotent(store):
    a = store.bind("t1", "c1", "m1")
    b = store.bind("t1", "c1", "m2")  # a restart re-binding must not reset the handle
    assert a.message_id == "m1"
    assert b.message_id == "m1"
    assert b.sequence == 0


def test_load_round_trips_handle_fields(store):
    h = store.bind("t1", "c1", "m1")
    h.sequence = 3
    h.state = CardState.RUNNING
    h.started_at = 42.0
    store.save(h)
    got = store.load("t1")
    assert (got.sequence, got.state, got.started_at) == (3, CardState.RUNNING, 42.0)
    assert got.message_id == "m1"


def test_load_missing_returns_none(store):
    assert store.load("nope") is None


def test_writer_requires_a_bound_handle(store):
    with pytest.raises(KeyError):
        store.writer("unbound")


def test_frames_persist_across_store_reopen(store, tmp_path):
    w = store.writer(store.bind("t1", "c1", "m1").task_id)
    w.emit(CardState.RUNNING, phase="build")
    w.emit(CardState.RUNNING, phase="cases")
    # A brand-new store instance = a restarted process reading the same file.
    reopened = CardStore(tmp_path / "cards.db")
    assert reopened.load("t1").sequence == 2


def test_restart_resumes_the_same_card_and_sequence(store, tmp_path):
    w = store.writer(store.bind("t1", "c1", "m1").task_id)
    w.emit(CardState.RUNNING, phase="build")

    resumed = CardStore(tmp_path / "cards.db").writer("t1")
    assert resumed.handle.message_id == "m1"
    frame = resumed.emit(CardState.RUNNING, phase="cases")
    assert frame.sequence == 2  # continues, never restarts at 1
    assert frame.uuid == "t1:2"


def test_terminal_state_survives_and_locks_after_restart(store, tmp_path):
    w = store.writer(store.bind("t1", "c1", "m1").task_id)
    w.emit(CardState.RUNNING, phase="build")
    w.emit(CardState.DONE, final=True)

    resumed = CardStore(tmp_path / "cards.db").writer("t1")
    assert resumed.handle.state is CardState.DONE
    # A late progress frame after restart must not resurrect the finished card.
    assert resumed.emit(CardState.RUNNING, phase="oops") is None


def test_dropped_frames_are_not_persisted(store, tmp_path):
    w = store.writer(store.bind("t1", "c1", "m1").task_id)
    w.emit(CardState.DONE, final=True)
    before = CardStore(tmp_path / "cards.db").load("t1").sequence
    w.emit(CardState.RUNNING)  # dropped
    assert CardStore(tmp_path / "cards.db").load("t1").sequence == before


def test_active_excludes_terminal_cards(store):
    store.writer(store.bind("a", "c", "m-a").task_id).emit(CardState.RUNNING)
    store.writer(store.bind("b", "c", "m-b").task_id).emit(CardState.DONE, final=True)
    store.writer(store.bind("d", "c", "m-d").task_id).emit(CardState.FAILED, final=True)
    assert [h.task_id for h in store.active()] == ["a"]


def test_forget_removes_the_handle(store):
    store.bind("t1", "c1", "m1")
    store.forget("t1")
    assert store.load("t1") is None
