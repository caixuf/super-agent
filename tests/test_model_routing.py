"""Tests for agentcore.model_routing deterministic router and effort ladders."""
from __future__ import annotations

import ast
from pathlib import Path
import sys
import unittest

from agentcore.completion_delivery import build_message
from agentcore.model_routing import (
    ARCHITECTURAL_DECISION_RECORDS,
    CODING_MODEL_ROUTE_SCHEMA_VERSION,
    COMPRESSION_PROTECTED_ROLES,
    DEFAULT_MAX_PRO_CALLS_PER_TASK,
    EXECUTION_ROLES,
    PRO_ALLOWED_ROLES,
    REASONING_EFFORT_LADDER,
    TIER_FLASH,
    TIER_MAIN,
    TIER_PRO,
    is_non_generative,
    normalize_effort,
    resolve_colleague_model,
    resolve_model_route,
)


class ModelRoutingTests(unittest.TestCase):
    def test_effort_normalization(self) -> None:
        self.assertEqual(normalize_effort("HIGH"), "high")
        self.assertEqual(normalize_effort("none"), "off")
        self.assertEqual(normalize_effort("0"), "off")
        self.assertEqual(normalize_effort("max"), "high")
        self.assertEqual(normalize_effort("unknown_rung"), "medium")

    def test_non_generative_filter(self) -> None:
        self.assertTrue(is_non_generative("text-embedding-3-small"))
        self.assertTrue(is_non_generative("bge-large-zh-v1.5"))
        self.assertTrue(is_non_generative("dall-e-3"))
        self.assertFalse(is_non_generative("gpt-4o"))
        self.assertFalse(is_non_generative("claude-3-7-sonnet"))

    def test_explicit_model_request_wins(self) -> None:
        route = resolve_model_route("hermes", requested_model="custom-coder-99b", requested_effort="high")
        self.assertEqual(route["schema_version"], CODING_MODEL_ROUTE_SCHEMA_VERSION)
        self.assertEqual(route["status"], "routed")
        self.assertEqual(route["selected_model"], "custom-coder-99b")
        self.assertEqual(route["selected_reasoning_effort"], "high")
        self.assertEqual(route["selected_tier"], "ultrabrain")
        self.assertEqual(route["provenance"], "request_named_model")
        self.assertFalse(route["degraded"])

    def test_explicit_non_generative_refused(self) -> None:
        route = resolve_model_route("hermes", requested_model="text-embedding-ada-002")
        self.assertEqual(route["status"], "model_refused")
        self.assertEqual(route["selected_model"], "")
        self.assertTrue(route["degraded"])

    def test_role_architect_escalates_to_ultrabrain(self) -> None:
        route = resolve_model_route("hermes", role="architect")
        self.assertEqual(route["status"], "routed")
        self.assertEqual(route["selected_model"], "o3-mini")
        self.assertEqual(route["selected_reasoning_effort"], "high")
        self.assertEqual(route["selected_tier"], "ultrabrain")
        self.assertEqual(route["provenance"], "role_chain_head")
        self.assertFalse(route["degraded"])

    def test_role_reviewer_escalates_to_deep(self) -> None:
        route = resolve_model_route("hermes", role="reviewer")
        self.assertEqual(route["status"], "routed")
        self.assertEqual(route["selected_reasoning_effort"], "high")
        self.assertEqual(route["selected_tier"], "deep")

    def test_role_test_downgrades_to_quick(self) -> None:
        route = resolve_model_route("hermes", role="test")
        self.assertEqual(route["status"], "routed")
        self.assertEqual(route["selected_model"], "gpt-4o-mini")
        self.assertEqual(route["selected_reasoning_effort"], "low")
        self.assertEqual(route["selected_tier"], "quick")

    def test_available_models_fallback(self) -> None:
        # If o3-mini is unavailable, fallback to gpt-4o and mark degraded=True (S2)
        route = resolve_model_route(
            "hermes",
            role="architect",
            available_models=["gpt-4o", "gpt-4o-mini"],
        )
        self.assertEqual(route["selected_model"], "gpt-4o")
        self.assertEqual(route["selected_tier"], "ultrabrain")
        self.assertTrue(route["degraded"])
        self.assertIn("unavailable", route["degradation_reason"])

    def test_executor_default_fallback(self) -> None:
        route = resolve_model_route("claude-code", role="unregistered_role")
        self.assertEqual(route["status"], "routed")
        self.assertEqual(route["selected_model"], "claude-3-5-sonnet")
        self.assertEqual(route["provenance"], "executor_default")
        self.assertTrue(route["degraded"])

    def test_s1_pro_tier_for_key_cognitive_roles(self) -> None:
        for role in ("architect", "planning", "reviewer", "triage", "deep_diagnosis"):
            route = resolve_colleague_model(role, "pro")
            self.assertEqual(route["selected_model"], TIER_PRO)
            self.assertFalse(route["degraded"])

    def test_s2_degradation_visibility_in_routing_and_completion(self) -> None:
        downgraded = resolve_colleague_model("ops", "pro")
        self.assertEqual(downgraded["selected_model"], TIER_FLASH)
        self.assertTrue(downgraded["degraded"])
        self.assertIn("BUDGET_GUARD", downgraded["degradation_reason"])

        msg = build_message({
            "job": "f" * 32,
            "status": "completed",
            "result": {
                "ok": True,
                "engine": "pi",
                "model_tier": "flash",
                "degraded": True,
                "engine_degraded": True,
                "degradation_reason": "primary_engine_fallback:hermes->turn_failed",
                "result": {"ok": True, "summary": "completed via fallback"},
            },
        })
        rendered = msg.render()
        self.assertTrue(rendered.get("degraded"))
        self.assertEqual(rendered.get("engine"), "pi")
        self.assertEqual(rendered.get("model_tier"), "flash")
        self.assertIn("primary_engine_fallback", rendered.get("degradation_reason", ""))

    def test_s3_compression_protected_from_flash_tier(self) -> None:
        self.assertNotIn("compression", EXECUTION_ROLES)
        for role in COMPRESSION_PROTECTED_ROLES:
            default_route = resolve_model_route("super-agent", role=role)
            self.assertEqual(default_route["selected_model"], TIER_MAIN)
            self.assertEqual(default_route["selected_reasoning_effort"], "medium")
            forced_flash = resolve_model_route("super-agent", role=role, requested_model="flash")
            self.assertEqual(forced_flash["selected_model"], TIER_MAIN)

    def test_s4_no_circular_import_from_agentcore_to_pi_executor(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        agentcore_dir = repo_root / "agentcore"
        forbidden = ("toolsets.pi_executor", "hermes_bridge.pi_executor")
        violations = []
        for py_file in sorted(agentcore_dir.glob("*.py")):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if any(alias.name.startswith(f) for f in forbidden):
                            violations.append(f"{py_file.name}:{node.lineno}:{alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    if any(node.module.startswith(f) for f in forbidden):
                        violations.append(f"{py_file.name}:{node.lineno}:{node.module}")
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr == "import_module" and node.args:
                        arg0 = node.args[0]
                        if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                            if any(arg0.value.startswith(f) for f in forbidden):
                                violations.append(f"{py_file.name}:{node.lineno}:{arg0.value}")
        self.assertEqual(violations, [], f"Circular imports detected in agentcore: {violations}")

    def test_s5_architectural_decision_records_and_set_invariants(self) -> None:
        for adr_id in ("ADR-S1", "ADR-S2", "ADR-S3", "ADR-S4", "ADR-S5", "ADR-S6"):
            self.assertIn(adr_id, ARCHITECTURAL_DECISION_RECORDS)
            self.assertTrue(ARCHITECTURAL_DECISION_RECORDS[adr_id].get("invariant"))
        self.assertTrue(PRO_ALLOWED_ROLES.isdisjoint(EXECUTION_ROLES))
        self.assertTrue(COMPRESSION_PROTECTED_ROLES.isdisjoint(EXECUTION_ROLES))
        self.assertEqual(DEFAULT_MAX_PRO_CALLS_PER_TASK, 2)

    def test_s6_single_card_producer_mutual_exclusion(self) -> None:
        from plugins.task_progress import _single_card_producer_conflict

        class FakeCtx:
            profile_name = "test-profile-s6"

            def __init__(self, cfg=None):
                self._cfg = cfg or {}

            def get_config(self, key, default=None):
                return self._cfg.get(key, default)

        producers = getattr(sys, "_superagent_active_card_producers", None)
        if not isinstance(producers, set):
            producers = set()
            setattr(sys, "_superagent_active_card_producers", producers)
        producers.add("test-profile-s6")
        try:
            self.assertTrue(_single_card_producer_conflict(FakeCtx()))
            self.assertFalse(
                _single_card_producer_conflict(FakeCtx({"allow_coexisting_card_producer": True}))
            )
        finally:
            producers.discard("test-profile-s6")


if __name__ == "__main__":
    unittest.main()

