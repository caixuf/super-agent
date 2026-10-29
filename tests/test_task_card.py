"""Task-card handle contract: ordering, idempotency, terminal locking and routing."""

from __future__ import annotations

import pytest

from agentcore.task_card import (
    CardFrame,
    CardHandle,
    CardState,
    CardWriter,
    LONG_JOB_SECONDS,
    RouteKind,
    route,
)


def _handle(**kw):
    base = dict(task_id="t1", chat_id="c1", message_id="m1")
    base.update(kw)
    return CardHandle(**base)


def test_emit_advances_sequence_monotonically():
    w = CardWriter(_handle())
    a = w.emit(CardState.RUNNING, phase="build")
    b = w.emit(CardState.RUNNING, phase="cases")
    assert (a.sequence, b.sequence) == (1, 2)
    assert w.handle.sequence == 2


def test_terminal_state_is_write_once():
    w = CardWriter(_handle())
    w.emit(CardState.RUNNING, phase="build")
    done = w.emit(CardState.DONE, final=True)
    assert done is not None
    # A late progress frame must not resurrect or overwrite the finished card.
    assert w.emit(CardState.RUNNING, phase="oops") is None
    assert w.handle.state is CardState.DONE
    assert w.handle.sequence == 2


def test_failure_is_terminal_and_not_rewritten_as_done():
    w = CardWriter(_handle())
    w.emit(CardState.FAILED, detail="runner exited 2", final=True)
    assert w.handle.state is CardState.FAILED
    assert w.emit(CardState.DONE, final=True) is None


def test_duplicate_uuid_is_dropped():
    w = CardWriter(_handle())
    first = w.emit(CardState.RUNNING, phase="build")
    # Replaying the exact same frame (same uuid) must not double-apply.
    assert w.commit(first) is None
    assert w.handle.sequence == 1


def test_stale_sequence_is_dropped():
    w = CardWriter(_handle())
    w.emit(CardState.RUNNING, phase="build")
    w.emit(CardState.RUNNING, phase="cases")
    stale = CardFrame(sequence=1, state=CardState.RUNNING, uuid="t1:1")
    assert w.commit(stale) is None
    assert w.handle.sequence == 2


def test_final_requires_terminal_state():
    w = CardWriter(_handle())
    with pytest.raises(ValueError):
        w.emit(CardState.RUNNING, final=True)


def test_uuid_is_deterministic_per_task_and_sequence():
    w = CardWriter(_handle(task_id="job-7"))
    f = w.emit(CardState.RUNNING)
    assert f.uuid == "job-7:1"


def test_heartbeat_keeps_a_quiet_card_alive():
    w = CardWriter(_handle())
    hb = w.heartbeat()
    assert hb is not None and hb.state is CardState.RUNNING
    assert w.emit(CardState.DONE, final=True) is not None
    assert w.heartbeat() is None  # terminal card stays quiet


def test_started_at_is_stamped_once():
    ticks = iter([100.0, 200.0])
    w = CardWriter(_handle(), clock=lambda: next(ticks))
    w.emit(CardState.RUNNING)
    w.emit(CardState.RUNNING)
    assert w.handle.started_at == 100.0


def test_frame_render_is_platform_agnostic():
    w = CardWriter(_handle())
    f = w.emit(CardState.RUNNING, phase="build", host="node-a", artifacts=("report.html",))
    view = f.render()
    assert view["state"] == "running"
    assert view["phase"] == "build"
    assert view["host"] == "node-a"
    assert view["artifacts"] == ["report.html"]
    assert view["final"] is False


def test_route_deterministic_hit_answers_inline():
    d = route(deterministic_hit=True, estimated_seconds=600)
    assert d.kind is RouteKind.LOOKUP
    assert not d.opens_card and not d.streams


def test_route_long_job_opens_a_card_and_does_not_stream():
    d = route(estimated_seconds=LONG_JOB_SECONDS)
    assert d.kind is RouteKind.LONG_JOB
    assert d.opens_card and not d.streams


def test_route_reasoning_turn_streams():
    d = route(needs_reasoning=True, estimated_seconds=1)
    assert d.kind is RouteKind.CONVERSATION
    assert d.streams and not d.opens_card


def test_route_unknown_duration_defaults_to_conversation():
    d = route()
    assert d.kind is RouteKind.CONVERSATION
