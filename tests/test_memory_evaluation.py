import json
from pathlib import Path

import pytest

from agentcore.memory_evaluation import evaluate


CASES = json.loads((Path(__file__).parent / "fixtures/memory_recall_cases.json").read_text())


def oracle():
    return {case["case_id"]: {"ids": case["relevant_ids"], "latency_ms": 5.0} for case in CASES}


def score(outputs):
    return evaluate(CASES, outputs, minimum_hit_rate=.9, maximum_false_positive_rate=.05,
                    maximum_p95_ms=100)


def test_independently_labelled_oracle_only_calibrates_scoring():
    result = score(oracle())
    assert result["passed"] and result["cases"] == 33
    assert result["hit_rate_at_k"] == result["mean_claim_recall"] == 1
    assert result["abstention_accuracy"] == 1 and result["forbidden_case_count"] == 0


def test_returning_all_similar_facts_exposes_silent_forbidden_recall():
    outputs = {case["case_id"]: {"ids": [fact["id"] for fact in case["facts"]],
                               "latency_ms": 2.0} for case in CASES}
    result = score(outputs)
    assert not result["passed"]
    assert result["forbidden_case_count"] > 0
    assert {"forbidden_recall", "false_positive_rate", "abstention"} <= set(result["failed_gates"])


def test_abstaining_from_everything_cannot_fake_safety_and_quality():
    outputs = {case["case_id"]: {"ids": [], "latency_ms": 1.0} for case in CASES}
    result = score(outputs)
    assert not result["passed"] and result["hit_rate_at_k"] == 0
    assert "hit_rate" in result["failed_gates"]


@pytest.mark.parametrize("replacement", [None, {"ids": [], "latency_ms": float("nan")},
                                          {"ids": [], "latency_ms": -1},
                                          {"ids": [], "latency_ms": True}])
def test_missing_or_invalid_outputs_do_not_count_as_correct_abstention(replacement):
    outputs = oracle()
    negative = next(case for case in CASES if case["expected_abstain"])
    outputs[negative["case_id"]] = replacement
    result = score(outputs)
    assert "incomplete_or_malformed_outputs" in result["failed_gates"]
    assert result["abstention_accuracy"] < 1


def test_latency_budget_is_checked_alongside_quality():
    outputs = oracle()
    for output in outputs.values():
        output["latency_ms"] = 500
    result = score(outputs)
    assert result["failed_gates"] == ["retrieval_latency"]


def test_duplicate_evidence_is_not_extra_recall_credit():
    case = next(case for case in CASES if len(case["relevant_ids"]) > 1 and
                len({fact["text"] for fact in case["facts"] if fact["id"] in case["relevant_ids"]}) == 1)
    outputs = oracle()
    outputs[case["case_id"]]["ids"] = [case["relevant_ids"][0]] * 6
    result = score(outputs)
    row = next(row for row in result["rows"] if row["case_id"] == case["case_id"])
    assert row["claim_recall"] == 1 and row["returned"] == 1 and row["duplicates"] == 5


def test_unknown_fact_id_and_extra_case_are_not_ignored():
    outputs = oracle()
    outputs[CASES[0]["case_id"]]["ids"] = ["fabricated-fact"]
    outputs["fabricated-case"] = {"ids": [], "latency_ms": 1}
    result = score(outputs)
    assert not result["passed"] and result["unknown_output_cases"] == ["fabricated-case"]
