"""Official Hermes Middleware Policy Implementation.

Standard Hermes plugin middleware for behavior shaping and access governance:
- C04: Tool availability & permission boundaries.
- C20: Model routing, resource budget, and provenance tracking.
- C21: High-risk operation interception (write protection & frontdesk isolation).
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

TASK_TOOLS = frozenset({
    "durable_submit", "durable_status", "durable_cancel", "durable_roster", "task_status",
    "colleague_list", "lan_node_list", "lan_node_probe",
    "skills_list", "skill_view", "feishu_doc_read", "web_extract",
    "kanban_create", "kanban_show", "kanban_list", "kanban_claim", "kanban_complete",
})

MEMORY_READ_TOOLS = frozenset({
    "memory_search_l1", "memory_timeline_l2", "memory_get_observations_l3",
})

TOOL_DENIED = "frontdesk_tool_denied: use durable task APIs; do not execute or delegate work in this profile."
WRITE_DENIED = "write_guard_denied: modifying protected files, deployment scripts, or credentials is prohibited."

# Sensitive path patterns that write guard protects unconditionally
PROTECTED_PATH_PATTERNS = (
    "*/.git/*",
    "*/.env",
    "*.pem",
    "*.key",
    "*/etc/systemd/*",
    "*/verify.sh",
    "*/deploy/*",
    "*/install/*",
)


class SuperAgentToolMiddleware:
    """Hermes tool_execution middleware enforcing frontdesk gates and write guards."""

    def __init__(
        self,
        *,
        frontdesk_enabled: bool = False,
        memory_read_enabled: bool = False,
        write_guard_enabled: bool = True,
        protected_patterns: tuple[str, ...] = PROTECTED_PATH_PATTERNS,
    ) -> None:
        self.frontdesk_enabled = frontdesk_enabled
        self.memory_read_enabled = memory_read_enabled
        self.write_guard_enabled = write_guard_enabled
        self.protected_patterns = protected_patterns

    def is_tool_allowed(self, tool_name: str) -> bool:
        if not self.frontdesk_enabled:
            return True
        allowed = set(TASK_TOOLS)
        if self.memory_read_enabled:
            allowed |= MEMORY_READ_TOOLS
        return tool_name in allowed

    def is_path_protected(self, path_str: str) -> bool:
        if not path_str or not isinstance(path_str, str):
            return False
        normalized = Path(path_str).as_posix()
        for pattern in self.protected_patterns:
            if fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(f"/{normalized}", pattern):
                return True
        return False

    def on_tool_request(
        self,
        tool_name: str,
        args: dict[str, Any],
        original_args: dict[str, Any],
        **context: Any,
    ) -> dict[str, Any]:
        """Tool request hook: validate tool name before execution."""
        if not self.is_tool_allowed(tool_name):
            # Intercept via middleware arguments if supported
            return {"args": args, "blocked": True, "reason": TOOL_DENIED}
        return {"args": args}

    def on_tool_execution(
        self,
        tool_name: str,
        args: dict[str, Any],
        original_args: dict[str, Any],
        next_call: Callable[[dict[str, Any]], Any],
        **context: Any,
    ) -> Any:
        """Tool execution hook: wrap or short-circuit tool calls (C04, C21)."""
        # 1. Frontdesk boundary enforcement (C02, C04)
        if not self.is_tool_allowed(tool_name):
            return {
                "ok": False,
                "error": TOOL_DENIED,
                "status": "blocked",
                "error_type": "frontdesk_policy",
            }

        # 2. Write Guard enforcement (C21)
        if self.write_guard_enabled and tool_name in ("write_file", "patch", "execute_code"):
            # Check target path arguments
            target_path = args.get("path") or args.get("file_path") or args.get("target") or args.get("TargetFile")
            if target_path and self.is_path_protected(str(target_path)):
                return {
                    "ok": False,
                    "error": WRITE_DENIED,
                    "status": "blocked",
                    "error_type": "write_guard",
                }

        # Safe to proceed through the chain
        return next_call(args)


class SuperAgentModelMiddleware:
    """Hermes llm_request middleware managing model tier routing & budgets (C20)."""

    def __init__(
        self,
        *,
        pro_model: str = "gpt-4o",
        flash_model: str = "gpt-4o-mini",
        max_token_budget: int = 16384,
    ) -> None:
        self.pro_model = pro_model
        self.flash_model = flash_model
        self.max_token_budget = max_token_budget

    def select_model_tier(self, prompt_text: str, requested_model: Optional[str] = None) -> tuple[str, str]:
        """Classify request into pro or flash tier based on task complexity."""
        if requested_model and "pro" in requested_model.lower():
            return self.pro_model, "explicit_pro_requested"

        # Heuristic complexity detection
        code_markers = ("def ", "class ", "```", "function", "SQL", "traceback", "unittest")
        complexity_score = sum(1 for m in code_markers if m in prompt_text)
        if len(prompt_text) > 4000 or complexity_score >= 2:
            return self.pro_model, "complex_reasoning_or_code"

        return self.flash_model, "low_latency_default"

    def on_llm_request(
        self,
        request: dict[str, Any],
        original_request: dict[str, Any],
        **context: Any,
    ) -> dict[str, Any]:
        """LLM request hook: shape model arguments and attach routing provenance (C20)."""
        updated = dict(request)
        messages = updated.get("messages", [])
        prompt_text = ""
        if messages and isinstance(messages, list):
            prompt_text = " ".join(
                str(m.get("content", "")) for m in messages if isinstance(m, dict)
            )

        requested_model = updated.get("model")
        selected_model, reason = self.select_model_tier(prompt_text, requested_model)
        updated["model"] = selected_model

        # Enforce max token budget
        max_tokens = updated.get("max_tokens")
        if max_tokens is None or max_tokens > self.max_token_budget:
            updated["max_tokens"] = self.max_token_budget

        return {
            "request": updated,
            "source": "superagent_middleware",
            "reason": reason,
            "requested_model": requested_model,
            "effective_model": selected_model,
        }


def register_superagent_middleware(ctx: Any, config: Optional[dict[str, Any]] = None) -> None:
    """Official plugin registration entrypoint for Hermes."""
    cfg = config or {}
    frontdesk_cfg = cfg.get("frontdesk", {})
    tool_mw = SuperAgentToolMiddleware(
        frontdesk_enabled=frontdesk_cfg.get("enabled", False),
        memory_read_enabled=frontdesk_cfg.get("memory_read_enabled", False),
        write_guard_enabled=cfg.get("write_guard_enabled", True),
    )
    model_mw = SuperAgentModelMiddleware(
        pro_model=cfg.get("pro_model", "gpt-4o"),
        flash_model=cfg.get("flash_model", "gpt-4o-mini"),
    )

    if hasattr(ctx, "register_middleware"):
        ctx.register_middleware("tool_execution", tool_mw.on_tool_execution)
        ctx.register_middleware("tool_request", tool_mw.on_tool_request)
        ctx.register_middleware("llm_request", model_mw.on_llm_request)
        logger.info("SuperAgent middleware suite successfully registered.")
