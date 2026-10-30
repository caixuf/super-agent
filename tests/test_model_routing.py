"""Tests for agentcore.model_routing deterministic router and effort ladders."""
from __future__ import annotations

import unittest

from agentcore.model_routing import (
    CODING_MODEL_ROUTE_SCHEMA_VERSION,
    REASONING_EFFORT_LADDER,
    is_non_generative,
    normalize_effort,
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

    def test_explicit_non_generative_refused(self) -> None:
        route = resolve_model_route("hermes", requested_model="text-embedding-ada-002")
        self.assertEqual(route["status"], "model_refused")
        self.assertEqual(route["selected_model"], "")

    def test_role_architect_escalates_to_ultrabrain(self) -> None:
        route = resolve_model_route("hermes", role="architect")
        self.assertEqual(route["status"], "routed")
        self.assertEqual(route["selected_model"], "o3-mini")
        self.assertEqual(route["selected_reasoning_effort"], "high")
        self.assertEqual(route["selected_tier"], "ultrabrain")
        self.assertEqual(route["provenance"], "role_chain_head")

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
        # If o3-mini is unavailable, fallback to gpt-4o
        route = resolve_model_route(
            "hermes",
            role="architect",
            available_models=["gpt-4o", "gpt-4o-mini"],
        )
        self.assertEqual(route["selected_model"], "gpt-4o")
        self.assertEqual(route["selected_tier"], "ultrabrain")

    def test_executor_default_fallback(self) -> None:
        route = resolve_model_route("claude-code", role="unregistered_role")
        self.assertEqual(route["status"], "routed")
        self.assertEqual(route["selected_model"], "claude-3-5-sonnet")
        self.assertEqual(route["provenance"], "executor_default")


if __name__ == "__main__":
    unittest.main()

