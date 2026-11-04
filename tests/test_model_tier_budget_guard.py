"""Unit tests for 3-tier model routing architecture and financial overdraft budget guardrails.

Validates:
1. Strict 3-tier architecture: pro (planning), main (primary dialog/routing), flash (execution).
2. Financial circuit breaker: execution roles are strictly forbidden from pro tier and downgraded to flash.
3. Whitelist enforcement: unapproved roles requesting pro tier are downgraded to main.
4. Task quota protection: pro calls capped per task to prevent runaway loop overdraft.
5. Dev runner and PlanGate integration guardrails.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import unittest

from agentcore.model_routing import (
    DEFAULT_MAX_PRO_CALLS_PER_TASK,
    EXECUTION_ROLES,
    PRO_ALLOWED_ROLES,
    TIER_FLASH,
    TIER_MAIN,
    TIER_PRO,
    enforce_model_tier_budget,
    resolve_model_route,
)
from agentcore.plan_gate import PlanGate, resolve_plan_route
from agentcore.dev_runner import run_dev_job, main as dev_runner_main


class ModelTierBudgetGuardTests(unittest.TestCase):
    def test_pro_whitelisted_roles_approved(self) -> None:
        """Whitelisted planning roles should safely receive pro tier within quota."""
        for role in PRO_ALLOWED_ROLES:
            route = resolve_model_route(
                "super-agent",
                role=role,
                requested_model="pro",
                task_pro_calls=0,
            )
            self.assertEqual(route["selected_model"], TIER_PRO, f"Role {role} failed to get pro")
            self.assertEqual(route["selected_tier"], "ultrabrain")
            self.assertEqual(route["selected_reasoning_effort"], "high")

    def test_execution_roles_strictly_downgraded_to_flash(self) -> None:
        """Execution roles requesting pro tier must be intercepted and forced to flash."""
        for exec_role in ("dev_runner", "test", "cases", "implementation", "worker", "ops"):
            route = resolve_model_route(
                "super-agent",
                role=exec_role,
                requested_model="pro",
                task_pro_calls=0,
            )
            self.assertEqual(
                route["selected_model"],
                TIER_FLASH,
                f"Execution role {exec_role} was not downgraded to flash!",
            )
            self.assertEqual(route["selected_tier"], "quick")
            # Verify explicit budget guard audit record
            guard_events = [ev for ev in route["attempted"] if ev.get("stage") == "budget_guard"]
            self.assertTrue(len(guard_events) > 0)
            self.assertIn("strictly prohibited", guard_events[0]["reason"])
            self.assertIn("financial overdraft", guard_events[0]["reason"])

    def test_unapproved_general_roles_downgraded_to_main(self) -> None:
        """General or unknown roles requesting pro tier must be downgraded to main."""
        for role in ("conversation", "general_assistant", "unregistered_custom_role"):
            route = resolve_model_route(
                "super-agent",
                role=role,
                requested_model="pro",
                task_pro_calls=0,
            )
            self.assertEqual(
                route["selected_model"],
                TIER_MAIN,
                f"Role {role} was not downgraded to main!",
            )
            self.assertEqual(route["selected_tier"], "deep")
            guard_events = [ev for ev in route["attempted"] if ev.get("stage") == "budget_guard"]
            self.assertTrue(len(guard_events) > 0)
            self.assertIn("not whitelisted", guard_events[0]["reason"])

    def test_task_pro_call_quota_exhaustion(self) -> None:
        """Exceeding max allowed pro calls per task must downgrade to main."""
        # Call 1: within quota (0 < 2)
        r1 = resolve_model_route("super-agent", role="planning", requested_model="pro", task_pro_calls=0, max_pro_calls=2)
        self.assertEqual(r1["selected_model"], TIER_PRO)

        # Call 2: within quota (1 < 2)
        r2 = resolve_model_route("super-agent", role="planning", requested_model="pro", task_pro_calls=1, max_pro_calls=2)
        self.assertEqual(r2["selected_model"], TIER_PRO)

        # Call 3: quota exhausted (2 >= 2) -> forced downgrade to main
        r3 = resolve_model_route("super-agent", role="planning", requested_model="pro", task_pro_calls=2, max_pro_calls=2)
        self.assertEqual(r3["selected_model"], TIER_MAIN)
        guard_events = [ev for ev in r3["attempted"] if ev.get("stage") == "budget_guard"]
        self.assertTrue(len(guard_events) > 0)
        self.assertIn("exceeded", guard_events[0]["reason"].lower())

    def test_super_agent_profile_role_chains(self) -> None:
        """Verify standard role chain mapping for the super-agent executor profile."""
        # Planning roles resolve to pro
        r_arch = resolve_model_route("super-agent", role="architect")
        self.assertEqual(r_arch["selected_model"], TIER_PRO)
        self.assertEqual(r_arch["selected_tier"], "ultrabrain")

        r_plan = resolve_model_route("super-agent", role="planning")
        self.assertEqual(r_plan["selected_model"], TIER_PRO)

        # Conversation / review roles resolve to main
        r_triage = resolve_model_route("super-agent", role="triage")
        self.assertEqual(r_triage["selected_model"], TIER_MAIN)
        self.assertEqual(r_triage["selected_tier"], "deep")

        r_rev = resolve_model_route("super-agent", role="reviewer")
        self.assertEqual(r_rev["selected_model"], TIER_MAIN)

        # Heavy execution roles resolve to flash
        r_cases = resolve_model_route("super-agent", role="cases")
        self.assertEqual(r_cases["selected_model"], TIER_FLASH)
        self.assertEqual(r_cases["selected_tier"], "quick")

        r_test = resolve_model_route("super-agent", role="test")
        self.assertEqual(r_test["selected_model"], TIER_FLASH)

        # Profile default fallback resolves to main
        r_default = resolve_model_route("super-agent")
        self.assertEqual(r_default["selected_model"], TIER_MAIN)

    def test_dev_runner_intercepts_pro_model(self, tmp_path: Path = None) -> None:
        """dev_runner run_dev_job must intercept model='pro' and force to 'flash'."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            workdir = base / "workdir"
            workdir.mkdir()
            agent_dir = base / "agent_dir"
            out_dir = base / "out"
            out_dir.mkdir()
            policy = {"allowed_roots": [str(workdir)]}
            job = {
                "job": "dev-guard-job",
                "attempt": "att-1",
                "payload": {"role": "cases", "goal": "Execute code check"},
            }

            recorded_kwargs = {}
            def mock_runner(prompt: str, kwargs: dict):
                recorded_kwargs.update(kwargs)
                return {"ok": True, "final_text": "done", "model": kwargs.get("model")}

            res = run_dev_job(
                job,
                policy,
                "# Contract",
                workdir,
                agent_dir,
                model="pro",  # Dangerous attempt to run dev_runner with pro
                runner_callable=mock_runner,
                output_dir=out_dir,
            )
            self.assertTrue(res["ok"])
            self.assertEqual(recorded_kwargs.get("model"), "flash")
            self.assertEqual(res["result"]["model"], "flash")

    def test_plan_gate_routes_and_enforces_quota(self) -> None:
        """PlanGate get_plan_route must use pro within budget, and downgrade when exhausted."""
        gate = PlanGate(max_pro_calls=2)

        # Call 1 for task-1
        r1 = gate.get_plan_route("task-101")
        self.assertEqual(r1["selected_model"], TIER_PRO)

        # Call 2 for task-1
        r2 = gate.get_plan_route("task-101")
        self.assertEqual(r2["selected_model"], TIER_PRO)

        # Call 3 for task-1: exhausted -> main
        r3 = gate.get_plan_route("task-101")
        self.assertEqual(r3["selected_model"], TIER_MAIN)

        # Independent task-2 has its own quota
        r_other = gate.get_plan_route("task-102")
        self.assertEqual(r_other["selected_model"], TIER_PRO)


if __name__ == "__main__":
    unittest.main()
