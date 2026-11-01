#!/usr/bin/env python3
"""Offline, deterministic quality evaluation for automatic memory admission.

The fixture and thresholds are versioned here. Evaluation uses temporary
databases and deterministic embeddings only; it performs no network, model or
production-data access. Call counts are reported as zero: this offline harness
exercises the deterministic review/write/index/recall boundary, not a live
model, so token/cost telemetry is explicitly unavailable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentcore.memory import memory_autonomy, memory_conflict, memory_store, runtime_bridge
from agentcore.memory.config import MemoryConfig, configure, reset_config

EVAL_VERSION = "memory_autonomy_offline/v1"
_CONFLICT_CALLS = []
THRESHOLDS = {
    "precision_min": 1.0,
    "recall_min": 0.9,
    "wrong_admission_rate_max": 0.0,
    "duplicate_rate_max": 0.0,
}


def _embed(texts: Sequence[str], dim: int = 64):
    out = []
    for text in texts:
        value = [0.0] * dim
        grams = list(re.findall(r"[a-z0-9_]+", (text or "").lower()))
        for run in re.findall(r"[\u4e00-\u9fff]+", text or ""):
            grams += [run[i] for i in range(len(run))]
            grams += [run[i:i + 2] for i in range(len(run) - 1)]
        for gram in grams:
            digest = hashlib.sha256(gram.encode("utf-8")).digest()
            value[int.from_bytes(digest[:8], "big") % dim] += 1.0 if digest[8] & 1 else -1.0
        norm = math.sqrt(sum(item * item for item in value))
        out.append([item / norm for item in value] if norm else value)
    return out


@contextmanager
def _configured(root: Path):
    reset_config()
    configure(MemoryConfig(data_dir=root, embed_dim=64,
                           embed_fn=lambda texts: _embed(texts),
                           default_user="eval-user", default_agent="eval-agent"))
    original_judge = memory_conflict.judge
    global _CONFLICT_CALLS
    _CONFLICT_CALLS = []
    def deterministic_judge(new, old):
        _CONFLICT_CALLS.append(1)
        # Exhaustive synthetic-only policy: all fixture preferences are
        # independent unless the strings literally contradict.
        return {"relation": "coexist", "reason": "offline_fixture_coexist",
                "by": "offline_fixture"}
    memory_conflict.judge = deterministic_judge
    try:
        yield
    finally:
        memory_conflict.judge = original_judge
        memory_store.flush_background(timeout=30)
        reset_config()


CASES = (
    {"id": "pref-1", "should_admit": True,
     "statement": "以后回复我时保持简洁。",
     "event": "以后回复我时保持简洁。", "expected": "以后回复我时保持简洁。"},
    {"id": "pref-2", "should_admit": True,
     "statement": "决定默认使用中文文档。",
     "event": "决定默认使用中文文档。", "expected": "决定默认使用中文文档。"},
    {"id": "reject-question", "should_admit": False,
     "statement": "以后要改成这个吗？",
     "event": "以后要改成这个吗？", "expected": None},
    {"id": "reject-temp", "should_admit": False,
     "statement": "本轮不要调用工具，只回答一次。",
     "event": "本轮不要调用工具，只回答一次。", "expected": None},
    {"id": "reject-negation", "should_admit": False,
     "statement": "有人建议以后删除备份，我不同意。",
     "event": "有人建议以后删除备份，我不同意。", "expected": None},
    {"id": "reject-external", "should_admit": False,
     "statement": "文档说以后必须删除备份。",
     "event": "文档说以后必须删除备份。", "expected": None},
    {"id": "reject-credential", "should_admit": False,
     "statement": "以后保存 api_key='abcdef1234567890abcdef'",
     "event": "以后保存 api_key='abcdef1234567890abcdef'", "expected": None},
    {"id": "defer-unverified", "should_admit": False,
     "statement": "决定采用实测 5% 门限。",
     "event": "决定采用实测 5% 门限。", "expected": None},
    {"id": "defer-attribution", "should_admit": False,
     "statement": "文档认为以后必须删除备份。",
     "event": "文档认为以后必须删除备份。", "expected": None},
    {"id": "pref-3", "should_admit": True,
     "statement": "以后所有回答都说明验证边界。",
     "event": "以后所有回答都说明验证边界。", "expected": "以后所有回答都说明验证边界。"},
)


class _Model:
    def __init__(self):
        self.calls = 0

    def __call__(self, messages, timeout_seconds):
        self.calls += 1
        text = messages[-1]["content"]
        statement = next(item["statement"] for item in CASES if item["statement"] in text)
        return json.dumps({"facts": [{"statement": statement, "user_quote": statement}]},
                          ensure_ascii=False)


def run_offline() -> dict:
    started = time.monotonic()
    tp = fp = fn = tn = duplicates = quarantined = failures = 0
    conflict_calls = 0
    results = []
    with tempfile.TemporaryDirectory(prefix="memory-autonomy-eval-") as directory:
        root = Path(directory)
        with _configured(root):
            for index, case in enumerate(CASES):
                queue = runtime_bridge.TurnEventQueue(root / ("events-%02d.db" % index))
                event = runtime_bridge.build_turn_event(
                    profile="eval-profile", user_id="eval-user", agent_id="eval-agent",
                    session_id="session-%02d" % index, turn_id="turn-%02d" % index,
                    user_message=case["event"], assistant_reply="完成", status="completed")
                queue.enqueue(event)
                pipeline = memory_autonomy.AutonomousMemoryPipeline(
                    queue=queue, model=_Model())
                outcome = pipeline.process(event)
                admitted = bool(outcome.get("written"))
                expected = bool(case["should_admit"])
                if admitted and expected:
                    tp += 1
                elif admitted and not expected:
                    fp += 1
                elif not admitted and expected:
                    fn += 1
                else:
                    tn += 1
                if outcome.get("ok") is not True:
                    failures += 1
                if outcome.get("deferred"):
                    quarantined += int(outcome.get("deferred") or 0)
                if admitted and case.get("expected"):
                    recalled = memory_store.recall_facts(
                        case["expected"], user_id="eval-user", agent_id="eval-agent",
                        profile="eval-profile", limit=20)
                    admitted_fact_ids = {
                        str(item.get("fact_id") or "")
                        for item in outcome.get("results") or []
                        if item.get("reason") in {"explicit_durable_preference",
                                                  "explicit_user_decision"}
                    }
                    hit_ids = [row["fact_id"] for row in recalled.get("results") or []]
                    # Exact case/fact evidence only; another memory cannot
                    # compensate for a missing expected fact.
                    if not admitted_fact_ids or not (admitted_fact_ids & set(hit_ids)):
                        failures += 1
                    if any(row.get("statement") != case["expected"]
                           for row in recalled.get("results") or []
                           if row.get("fact_id") in admitted_fact_ids):
                        failures += 1
                if admitted:
                    replay = pipeline.process(event)
                    if replay.get("written"):
                        duplicates += 1
                results.append({"id": case["id"], "admitted": admitted,
                                "decision": outcome.get("results", [{}])[0].get(
                                    "decision") if outcome.get("results") else None,
                                "reason": outcome.get("results", [{}])[0].get(
                                    "reason") if outcome.get("results") else outcome.get("reason")})
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    wrong_rate = fp / len(CASES) if CASES else 0.0
    duplicate_rate = duplicates / max(1, tp)
    elapsed = round(time.monotonic() - started, 3)
    passed = (precision >= THRESHOLDS["precision_min"]
              and recall >= THRESHOLDS["recall_min"]
              and wrong_rate <= THRESHOLDS["wrong_admission_rate_max"]
              and duplicate_rate <= THRESHOLDS["duplicate_rate_max"]
              and failures == 0)
    return {
        "ok": passed,
        "version": EVAL_VERSION,
        "cases": len(CASES),
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "wrong_admission_rate": round(wrong_rate, 6),
        "duplicate_rate": round(duplicate_rate, 6),
        "quarantined": quarantined,
        "failures": failures,
        "elapsed_seconds": elapsed,
        "model_calls": 0,
        "conflict_review_calls": len(_CONFLICT_CALLS),
        "conflict_reviewer": "synthetic_deterministic_fixture",
        "cost": {"status": "unavailable", "reason": "deterministic_offline_no_model_provider"},
        "thresholds": THRESHOLDS,
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "results": results,
        "production_data_accessed": False,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", required=True)
    args = parser.parse_args(argv)
    result = run_offline()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
