import copy
import json
import subprocess
import sys

import pytest

from agentcore.acceptance_evidence import evaluate


def passing_record():
    events = []
    def add(kind, at, **fields):
        source = ("queue" if kind in {"task_probe", "task_transition"} else
                  "transport" if kind == "message_out" else "gateway")
        event = {"id": f"e{len(events)}", "type": kind, "source": source, "at": at,
                 "session_id": "session-a", "route_key": "profile/user/chat", **fields}
        events.append(event)
        return event
    add("message_in", 1, origin="user", message_id="analysis", content_hash="hash-a")
    add("turn_start", 1.1, origin="user", lane="main", turn_id="turn-a", message_ids=["analysis"])
    add("tool_call", 1.2, turn_id="turn-a", tool_name="durable_submit", call_id="call-a")
    add("tool_result", 1.3, turn_id="turn-a", tool_name="durable_submit", call_id="call-a",
        task_id="a" * 32, ok=True)
    add("message_out", 1.4, turn_id="turn-a", reply_to="analysis", final=True,
        delivery_status="confirmed", transport_receipt="platform-receipt-a", text="task accepted")
    add("turn_end", 1.5, turn_id="turn-a")
    add("message_in", 3, origin="user", message_id="math", content_hash="hash-b")
    add("turn_start", 3.1, origin="user", lane="main", turn_id="turn-b", message_ids=["math"])
    reply = add("message_out", 4, turn_id="turn-b", reply_to="math", final=True,
                delivery_status="confirmed", transport_receipt="platform-receipt-b", text="43")
    add("turn_end", 4.2, turn_id="turn-b")
    for position, at in (("arrival", 2.99), ("before", 3.9), ("after", 4.1)):
        add("task_probe", at, position=position, reply_event_id=reply["id"], task_id="a" * 32,
            attempt="attempt-a", status="running", owner_session_id="session-a", message_id="math")
    requests = {"analysis": {"kind": "analysis", "max_reply_seconds": 10},
                "math": {"kind": "quick", "expected_text": "43", "max_reply_seconds": 2,
                         "while_task_from": "analysis", "scenario": "background_interjection"}}
    for i in range(3):
        mid, tid = f"burst-{i}", f"burst-turn-{i}"
        route = {"session_id": "session-burst", "route_key": "profile/burst/chat"}
        add("message_in", 5 + .1*i, origin="user", message_id=mid, content_hash=mid, **route)
        add("turn_start", 5.3 + .3*i, origin="user", lane="main", turn_id=tid, message_ids=[mid], **route)
        add("message_out", 5.4 + .3*i, turn_id=tid, reply_to=mid, final=True,
            delivery_status="confirmed", transport_receipt=mid, text=mid, **route)
        add("turn_end", 5.5 + .3*i, turn_id=tid, **route)
        requests[mid] = {"kind": "quick", "expected_text": mid, "max_reply_seconds": 2,
                         "scenario": "same_session_burst"}
    for i in range(2):
        mid, tid = f"cross-{i}", f"cross-turn-{i}"
        route = {"session_id": f"session-cross-{i}", "route_key": f"profile/cross-{i}/chat"}
        add("message_in", 7, origin="user", message_id=mid, content_hash=mid, **route)
        add("turn_start", 7.1, origin="user", lane="main", turn_id=tid, message_ids=[mid], **route)
        add("message_out", 7.5, turn_id=tid, reply_to=mid, final=True,
            delivery_status="confirmed", transport_receipt=mid, text=mid, **route)
        add("turn_end", 7.6, turn_id=tid, **route)
        requests[mid] = {"kind": "quick", "expected_text": mid, "max_reply_seconds": 2,
                         "scenario": "cross_session"}
    return {"schema_version": 2,
            "capture": {"opened_at": 0, "closed_at": 10, "complete": True,
                        "clock": "host_monotonic", "sources": ["gateway", "queue", "transport"]},
            "requests": requests, "events": events}


def event(record, kind, **fields):
    return next(item for item in record["events"] if item["type"] == kind and
                all(item.get(key) == value for key, value in fields.items()))


def failed(record, name):
    result = evaluate(record)
    assert not result["passed"]
    assert name in result["failed_gates"], result


def test_complete_trusted_normalized_trace_is_consistent():
    result = evaluate(passing_record())
    assert result["passed"], result
    assert result["latencies"]["math"] == 1
    assert result["durable_task_ids"] == ["a" * 32]


@pytest.mark.parametrize("field,value", [("session_id", "other"), ("route_key", "other/chat")])
def test_actual_reply_cannot_borrow_another_session_or_target(field, value):
    record = passing_record()
    event(record, "message_out", reply_to="math")[field] = value
    failed(record, "reply_route_or_delivery")


def test_missing_reply_is_not_rescued_by_transcript_text():
    record = passing_record()
    item = event(record, "message_out", reply_to="math")
    item.update(type="transcript_message", source="gateway", text="43")
    failed(record, "missing_or_duplicate_reply")


def test_double_reply_is_rejected():
    record = passing_record()
    duplicate = copy.deepcopy(event(record, "message_out", reply_to="math"))
    duplicate["id"] = "second-output"
    duplicate["transport_receipt"] = "second-platform-message"
    record["events"].append(duplicate)
    failed(record, "missing_or_duplicate_reply")


def test_raw_transport_redelivery_is_one_logical_request():
    record = passing_record()
    duplicate = copy.deepcopy(event(record, "message_in", message_id="math"))
    duplicate["id"] = "redelivery"
    record["events"].append(duplicate)
    assert evaluate(record)["passed"]
    duplicate["content_hash"] = "changed"
    failed(record, "conflicting_redelivery")


@pytest.mark.parametrize("change", ["completed", "attempt", "different-task", "missing-after", "stale-probe"])
def test_running_overlap_requires_current_bracketed_same_attempt_observations(change):
    record = passing_record()
    probe = event(record, "task_probe", position="after")
    if change == "completed":
        probe["status"] = "completed"
    elif change == "attempt":
        probe["attempt"] = "new-attempt"
    elif change == "different-task":
        probe["task_id"] = "b" * 32
    elif change == "missing-after":
        record["events"].remove(probe)
    else:
        event(record, "task_probe", position="before")["at"] = 1.5
    failed(record, "reply_while_task_running")


@pytest.mark.parametrize("change", ["wrong-call", "wrong-session", "temporary-task", "only-mentioned"])
def test_durable_result_must_match_actual_call_and_owner(change):
    record = passing_record()
    result = event(record, "tool_result")
    if change == "wrong-call":
        result["call_id"] = "unrelated-call"
    elif change == "wrong-session":
        result["session_id"] = "other-session"
    elif change == "temporary-task":
        result["task_id"] = "deleg_temporary"
    else:
        event(record, "tool_call")["tool_name"] = "delegate_task"
    failed(record, "durable_submit_actual_call")


def test_completion_cannot_claim_main_user_lane():
    record = passing_record()
    common = {"source": "gateway", "session_id": "session-a", "route_key": "profile/user/chat",
              "turn_id": "completion-turn"}
    record["events"] += [dict(common, id="completion-start", type="turn_start", at=2,
                              origin="completion", lane="main", message_ids=[]),
                         dict(common, id="completion-stop", type="turn_end", at=2.1)]
    failed(record, "completion_occupies_main_turn")


def test_ambiguous_transport_outcome_is_not_confirmed_delivery():
    record = passing_record()
    event(record, "message_out", reply_to="math")["delivery_status"] = "unknown"
    failed(record, "reply_route_or_delivery")


def test_correct_but_slow_answer_fails_explicit_latency_budget():
    record = passing_record()
    record["requests"]["math"]["max_reply_seconds"] = .5
    failed(record, "reply_latency_budget")


def test_answer_content_and_all_three_scenario_shapes_matter():
    record = passing_record()
    event(record, "message_out", reply_to="math")["text"] = "42"
    failed(record, "incorrect_reply")
    for spec in record["requests"].values():
        if spec.get("scenario") in {"same_session_burst", "cross_session"}:
            spec.pop("scenario")
    result = evaluate(record)
    assert {"scenario_same_session_burst", "scenario_cross_session"} <= set(result["failed_gates"])


@pytest.mark.parametrize("change", ["capture", "nan", "wrong-source", "duplicate-id", "unhashable"])
def test_malformed_or_partial_capture_fails_closed(change):
    record = passing_record()
    if change == "capture":
        record["capture"]["complete"] = False
    elif change == "nan":
        record["events"][0]["at"] = float("nan")
    elif change == "wrong-source":
        event(record, "task_probe")["source"] = "assistant"
    elif change == "duplicate-id":
        record["events"][1]["id"] = record["events"][0]["id"]
    else:
        record["capture"]["sources"] = [{}]
    assert not evaluate(record)["passed"]


def test_cli_failure_returns_nonzero_for_automation():
    result = subprocess.run([sys.executable, "-m", "agentcore.acceptance_evidence"],
                            input=json.dumps({}), text=True, capture_output=True)
    assert result.returncode == 1
    assert json.loads(result.stdout)["passed"] is False


def test_cross_session_requests_must_actually_overlap_not_just_exist():
    record = passing_record()
    for item in record["events"]:
        if item.get("session_id") == "session-cross-1":
            item["at"] += 1
    failed(record, "scenario_cross_session")
