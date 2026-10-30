"""Consistency audit for a complete, trusted conversation trace, never a proof of delivery by itself."""
from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter
from typing import Any


#: Which authoritative source may emit each event type. Single source of truth:
#: the recorder (``agentcore.acceptance_capture``) validates against this same map,
#: so a capture it produces can never be rejected for a mislabelled source.
EVENT_SOURCES = {"message_in": "gateway", "turn_start": "gateway", "turn_end": "gateway",
                 "tool_call": "gateway", "tool_result": "gateway", "message_out": "transport",
                 "task_probe": "queue", "task_transition": "queue", "transcript_message": "gateway"}

#: The three sources a complete capture must span; a capture missing one cannot
#: distinguish "nothing happened" from "nobody was watching".
REQUIRED_SOURCES = frozenset({"gateway", "queue", "transport"})


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def evaluate(record: dict[str, Any]) -> dict[str, Any]:
    try:
        return _evaluate(record)
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
        return {"passed": False, "failed_gates": ["malformed_evidence"], "latencies": {}}


def _evaluate(record: dict[str, Any]) -> dict[str, Any]:
    failures = set()
    def fail(name):
        failures.add(name)
    if not isinstance(record, dict) or record.get("schema_version") != 2:
        return {"passed": False, "failed_gates": ["unsupported_schema"], "latencies": {}}
    capture = record.get("capture", {})
    if not isinstance(capture, dict):
        capture = {}
    lower, upper = capture.get("opened_at"), capture.get("closed_at")
    if (capture.get("complete") is not True or not _number(lower) or not _number(upper)
            or upper < lower or capture.get("clock") != "host_monotonic"
            or not REQUIRED_SOURCES.issubset(set(capture.get("sources", [])))):
        fail("incomplete_capture")
    raw = record.get("events")
    events = []
    if not isinstance(raw, list):
        fail("invalid_events")
        raw = []
    allowed = EVENT_SOURCES
    ids = set()
    for event in raw:
        if (not isinstance(event, dict) or event.get("type") not in allowed
                or event.get("source") != allowed.get(event.get("type"))
                or not isinstance(event.get("id"), str) or not event["id"]
                or not _number(event.get("at"))):
            fail("invalid_event")
            continue
        if event["id"] in ids:
            fail("duplicate_event_id")
        ids.add(event["id"])
        if _number(lower) and _number(upper) and not lower <= event["at"] <= upper:
            fail("event_outside_capture")
        events.append(event)
    def select(kind):
        return [event for event in events if event["type"] == kind]
    def identity(event):
        return event.get("session_id"), event.get("route_key")
    incoming = {}
    for event in select("message_in"):
        key = event.get("message_id")
        if (event.get("origin") != "user" or not isinstance(key, str) or not key
                or not all(isinstance(value, str) and value for value in identity(event))):
            fail("invalid_inbound_identity")
            continue
        if key in incoming and (identity(event) != identity(incoming[key])
                                or event.get("content_hash") != incoming[key].get("content_hash")):
            fail("conflicting_redelivery")
        incoming.setdefault(key, event)  # transport redelivery is one logical request
    specs = record.get("requests", {})
    if not isinstance(specs, dict) or not specs or set(specs) != set(incoming):
        fail("request_coverage")
        if not isinstance(specs, dict):
            specs = {}
    starts, ends = select("turn_start"), select("turn_end")
    turns = {}
    for start in starts:
        turn_id = start.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id or turn_id in turns:
            fail("duplicate_or_missing_turn")
            continue
        turns[turn_id] = start
        if start.get("origin") not in {"user", "completion"} or start.get("lane") not in {"main", "background"}:
            fail("invalid_turn_origin")
        if start.get("origin") == "user" and start.get("lane") != "main":
            fail("invalid_turn_origin")
        if start.get("origin") != "user" and start.get("lane") == "main":
            fail("completion_occupies_main_turn")
        stop = [end for end in ends if end.get("turn_id") == turn_id]
        if len(stop) != 1 or identity(stop[0]) != identity(start) or stop[0]["at"] < start["at"]:
            fail("turn_lifecycle")
        message_ids = start.get("message_ids", [])
        if not isinstance(message_ids, list) or any(not isinstance(mid, str) for mid in message_ids):
            fail("invalid_turn_messages")
    outs = select("message_out")
    if any(end.get("turn_id") not in turns for end in ends):
        fail("orphan_turn_end")
    latencies, replies, admitted = {}, {}, {}
    for mid, inbound in incoming.items():
        assigned = [start for start in starts if isinstance(start.get("message_ids"), list)
                    and mid in start["message_ids"] and start.get("origin") == "user"]
        if len(assigned) != 1:
            fail("message_admission_count")
        elif identity(assigned[0]) != identity(inbound) or assigned[0]["at"] < inbound["at"]:
            fail("admission_route_or_time")
        else:
            admitted[mid] = assigned[0]
        final = [out for out in outs if out.get("reply_to") == mid and out.get("final") is True]
        if len(final) != 1:
            fail("missing_or_duplicate_reply")
            continue
        out = final[0]
        if (out.get("delivery_status") != "confirmed" or not out.get("transport_receipt")
                or identity(out) != identity(inbound) or out["at"] < inbound["at"]
                or out.get("turn_id") != admitted.get(mid, {}).get("turn_id")):
            fail("reply_route_or_delivery")
            continue
        replies[mid] = out
        latencies[mid] = out["at"] - inbound["at"]
        spec = specs.get(mid, {})
        if not isinstance(spec, dict):
            fail("invalid_request_spec")
            continue
        budget = spec.get("max_reply_seconds")
        if not _number(budget) or budget <= 0:
            fail("missing_latency_budget")
        elif latencies[mid] > budget:
            fail("reply_latency_budget")
        if "expected_text" in spec and str(out.get("text", "")).strip() != str(spec["expected_text"]).strip():
            fail("incorrect_reply")
    for out in outs:
        if out.get("final") is True and out.get("reply_to") not in incoming:
            fail("orphan_reply")
    for start in starts:
        for mid in start.get("message_ids", []) if isinstance(start.get("message_ids"), list) else []:
            if mid not in incoming:
                fail("orphan_admission")
    for session in {identity(start) for start in starts if start.get("lane") == "main"}:
        chain = sorted((start for start in starts if identity(start) == session and start.get("lane") == "main"),
                       key=lambda event: event["at"])
        last_end, last_inbound = -math.inf, -math.inf
        for start in chain:
            stop = [end for end in ends if end.get("turn_id") == start.get("turn_id")]
            if start["at"] < last_end:
                fail("overlapping_main_turns")
            if len(stop) == 1:
                last_end = stop[0]["at"]
            mids = start.get("message_ids", [])
            times = [incoming[mid]["at"] for mid in mids if isinstance(mid, str) and mid in incoming] if isinstance(mids, list) else []
            if times and (times != sorted(times) or min(times) < last_inbound):
                fail("session_fifo_order")
            if times:
                last_inbound = max(times)
    calls = select("tool_call")
    if any(count > 1 for count in Counter(event.get("call_id") for event in calls).values()):
        fail("duplicate_tool_call_id")
    durable = {}
    for mid, spec in specs.items():
        if not isinstance(spec, dict) or spec.get("kind") != "analysis":
            continue
        start = admitted.get(mid)
        if not start:
            continue
        submitted = [call for call in calls if call.get("turn_id") == start.get("turn_id")
                     and call.get("tool_name") == "durable_submit" and call.get("call_id")]
        matching = []
        for call in submitted:
            results = [result for result in select("tool_result") if result.get("call_id") == call["call_id"]]
            if len(results) != 1:
                continue
            result = results[0]
            if (result.get("tool_name") == "durable_submit" and result.get("ok") is True
                    and identity(result) == identity(call) == identity(start)
                    and result.get("turn_id") == start.get("turn_id")
                    and result["at"] >= call["at"] >= start["at"]
                    and isinstance(result.get("task_id"), str)
                    and re.fullmatch(r"[0-9a-f]{32}", result["task_id"])):
                matching.append(result)
        if len(matching) != 1:
            fail("durable_submit_actual_call")
        else:
            durable[mid] = matching[0]
            if mid in replies and replies[mid]["at"] < matching[0]["at"]:
                fail("receipt_before_commit")
    for mid, spec in specs.items():
        if not isinstance(spec, dict) or not spec.get("while_task_from"):
            continue
        owner = spec["while_task_from"]
        result, reply = durable.get(owner), replies.get(mid)
        if not result or not reply or identity(reply) != identity(result):
            fail("reply_while_task_running")
            continue
        probes = [probe for probe in select("task_probe") if probe.get("reply_event_id") == reply["id"]]
        before = [probe for probe in probes if probe.get("position") == "before"]
        after = [probe for probe in probes if probe.get("position") == "after"]
        arrival = [probe for probe in probes if probe.get("position") == "arrival"
                   and probe.get("message_id") == mid]
        if len(before) != 1 or len(after) != 1 or len(arrival) != 1:
            fail("reply_while_task_running")
            continue
        left, right, first = before[0], after[0], arrival[0]
        limit = record.get("max_probe_window_seconds", 1.0)
        inbound = incoming[mid]
        valid = (_number(limit) and limit > 0 and result["at"] <= left["at"] <= reply["at"] <= right["at"]
                 and right["at"] - left["at"] <= limit and left.get("attempt")
                 and left.get("attempt") == right.get("attempt") == first.get("attempt")
                 and result["at"] <= first["at"] <= inbound["at"] <= first["at"] + limit)
        for probe in (first, left, right):
            valid = valid and (probe.get("task_id") == result["task_id"] and probe.get("status") == "running"
                               and probe.get("owner_session_id") == result.get("session_id")
                               and probe.get("route_key") == result.get("route_key"))
        transitions = [event for event in select("task_transition") if event.get("task_id") == result["task_id"]
                       and first["at"] <= event["at"] <= right["at"]]
        if not valid or any(event.get("status") != "running" for event in transitions):
            fail("reply_while_task_running")
    # These are the acceptance contract, not caller-selectable checkboxes.
    tags = ["same_session_burst", "cross_session", "background_interjection"]
    for tag in tags:
        mids = [mid for mid, spec in specs.items() if isinstance(spec, dict) and spec.get("scenario") == tag]
        if tag == "same_session_burst" and (len(mids) < 3 or len({identity(incoming[mid]) for mid in mids if mid in incoming}) != 1):
            fail("scenario_same_session_burst")
        elif tag == "same_session_burst":
            stops = [end["at"] for mid in mids for end in ends
                     if end.get("turn_id") == admitted.get(mid, {}).get("turn_id")]
            if len(stops) != len(mids) or max(incoming[mid]["at"] for mid in mids) >= min(stops):
                fail("scenario_same_session_burst")
        elif tag == "cross_session":
            windows = [(identity(incoming[mid]), admitted[mid]["at"], end["at"])
                       for mid in mids if mid in incoming and mid in admitted
                       for end in ends if end.get("turn_id") == admitted[mid].get("turn_id")]
            if not any(a[0] != b[0] and max(a[1], b[1]) < min(a[2], b[2])
                       for a in windows for b in windows):
                fail("scenario_cross_session")
        elif tag == "background_interjection" and not any(specs[mid].get("while_task_from") for mid in mids):
            fail("scenario_background_interjection")
        elif tag not in {"same_session_burst", "cross_session", "background_interjection"}:
            fail("unknown_scenario")
    return {"passed": not failures, "failed_gates": sorted(failures), "latencies": latencies,
            "durable_task_ids": sorted(result["task_id"] for result in durable.values()),
            "logical_inbound_count": len(incoming),
            "limitation": "Trace consistency only; capture authenticity, client rendering, cancellation and recall quality are separate gates."}


if __name__ == "__main__":
    result = evaluate(json.load(sys.stdin))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["passed"] else 1)
