"""Score captured retrieval outputs against independently labelled, offline cases.

This does not retrieve, authorize, write, or promote memories. A caller must run
its real retriever separately, preserving case IDs and measured latency. An
oracle fixture calibrates the scorer, never establishes retrieval quality.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any


def _number(value):
    return type(value) in (float, int) and math.isfinite(value) and value >= 0


def _percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * fraction) - 1)]


def evaluate(cases: list[dict[str, Any]], outputs: dict[str, Any], *, top_k: int = 6,
             minimum_hit_rate: float, maximum_false_positive_rate: float,
             maximum_p95_ms: float) -> dict[str, Any]:
    if type(top_k) is not int or top_k < 1:
        raise ValueError("top_k must be a positive integer")
    for value in (minimum_hit_rate, maximum_false_positive_rate):
        if not _number(value) or value > 1:
            raise ValueError("rate thresholds must be in [0, 1]")
    if not _number(maximum_p95_ms):
        raise ValueError("an explicit non-negative latency budget is required")
    if not isinstance(cases, list) or not cases or not isinstance(outputs, dict):
        raise ValueError("non-empty labelled cases and captured outputs are required")
    seen = set()
    rows, latency = [], []
    labels = {}
    for case in cases:
        cid = case["case_id"]
        if not isinstance(cid, str) or not cid or cid in seen:
            raise ValueError("case IDs must be unique")
        seen.add(cid)
        facts = {fact["id"]: fact for fact in case["facts"]}
        if len(facts) != len(case["facts"]):
            raise ValueError("fact IDs must be unique within a case")
        relevant, forbidden = set(case["relevant_ids"]), set(case["forbidden_ids"])
        if relevant & forbidden or not (relevant | forbidden) <= facts.keys():
            raise ValueError("invalid or overlapping labels")
        abstain = case["expected_abstain"]
        if type(abstain) is not bool or abstain != (not relevant):
            raise ValueError("abstention label must match the absence of a supported answer")
        labels[cid] = (case, facts, relevant, forbidden)
    unknown_outputs = sorted(set(outputs) - seen)
    malformed = []
    for cid, (case, facts, relevant, forbidden) in labels.items():
        output = outputs.get(cid)
        valid = (isinstance(output, dict) and isinstance(output.get("ids"), list)
                 and all(isinstance(fid, str) for fid in output["ids"])
                 and _number(output.get("latency_ms")))
        if not valid:
            malformed.append(cid)
            # Missing/error outputs are not scored as a correct abstention.
            rows.append({"case_id": cid, "valid": False, "positive": bool(relevant),
                         "hit": False, "abstention_correct": False, "claim_recall": 0.0,
                         "returned": 0, "false_positive": 0, "forbidden": [], "duplicates": 0})
            continue
        raw = output["ids"][:top_k]
        ids = list(dict.fromkeys(raw))
        returned = set(ids)
        latency.append(output["latency_ms"])
        # Equal labelled statements are alternative evidence for one claim,
        # rather than extra targets that artificially punish deduplication.
        groups = defaultdict(set)
        for fid in relevant:
            text = " ".join(facts[fid]["text"].casefold().split())
            groups[text].add(fid)
        recall = (sum(bool(returned & group) for group in groups.values()) / len(groups)
                  if groups else 0.0)
        rows.append({"case_id": cid, "valid": True, "positive": bool(relevant),
                     "hit": bool(returned & relevant),
                     "abstention_correct": not returned if case["expected_abstain"] else bool(returned),
                     "claim_recall": recall, "returned": len(returned),
                     "false_positive": len(returned - relevant),
                     "forbidden": sorted(returned & forbidden),
                     "unknown_ids": sorted(returned - facts.keys()),
                     "duplicates": len(raw) - len(ids)})
    positives = [row for row in rows if row["positive"]]
    negatives = [row for row in rows if not row["positive"]]
    hit_rate = sum(row["hit"] for row in positives) / len(positives) if positives else None
    returned = sum(row["returned"] for row in rows)
    fp = sum(row["false_positive"] for row in rows)
    fp_rate = fp / returned if returned else 0.0
    leaks = sum(bool(row["forbidden"]) for row in rows)
    failures = []
    if malformed or unknown_outputs:
        failures.append("incomplete_or_malformed_outputs")
    if hit_rate is None or hit_rate < minimum_hit_rate:
        failures.append("hit_rate")
    if fp_rate > maximum_false_positive_rate:
        failures.append("false_positive_rate")
    if leaks:
        failures.append("forbidden_recall")
    p95 = _percentile(latency, .95)
    if p95 is None or p95 > maximum_p95_ms:
        failures.append("retrieval_latency")
    if any(not row["abstention_correct"] for row in negatives):
        failures.append("abstention")
    return {"passed": not failures, "failed_gates": failures, "cases": len(rows),
            "hit_rate_at_k": hit_rate, "false_positive_rate": fp_rate,
            "mean_claim_recall": (sum(row["claim_recall"] for row in positives) / len(positives)
                                  if positives else None),
            "abstention_accuracy": (sum(row["abstention_correct"] for row in negatives) / len(negatives)
                                    if negatives else None),
            "forbidden_case_count": leaks,
            "latency_ms": {"p50": _percentile(latency, .5), "p95": p95,
                           "maximum": max(latency, default=None)},
            "missing_or_malformed_cases": malformed, "unknown_output_cases": unknown_outputs,
            "rows": rows,
            "limitation": "Offline labelled-set performance; not a production-quality or authorization proof."}
