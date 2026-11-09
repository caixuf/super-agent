"""Deterministic model routing and reasoning effort hierarchy for Super-Agent.

Pure-function decision engine: same inputs, byte-identical payload, no I/O, no clock.
Supports multi-tier reasoning depth (ultrabrain / deep / standard / quick) and
role-based execution chains adapted from open-source orchestration standards.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Final, Iterable, List, Mapping, Optional, Sequence, Tuple

CODING_MODEL_ROUTE_SCHEMA_VERSION: Final[str] = "coding_model_route/v2"

# 3-Tier Model Architecture Constants
TIER_PRO: Final[str] = "pro"
TIER_MAIN: Final[str] = "main"
TIER_FLASH: Final[str] = "flash"
MODEL_TIERS: Final[Tuple[str, ...]] = (TIER_PRO, TIER_MAIN, TIER_FLASH)

# Strict whitelist for expensive 'pro' model tier (planning, architecture, review & deep diagnosis)
PRO_ALLOWED_ROLES: Final[frozenset[str]] = frozenset({
    "architect",
    "brain",
    "planning",
    "plan_gate",
    "plan_generator",
    "deep_diagnosis",
    "reviewer",
    "triage",
    "maintainer",
})

# Roles strictly forbidden from accessing 'pro' tier (forced to flash to prevent overdraft).
# NOTE (ADR-S3): 'compression' / 'memory' / 'context_compaction' must NEVER be placed in
# EXECUTION_ROLES because forcing long-context compaction onto the lowest 'flash' tier
# irreversibly corrupts session memory and architectural state.
EXECUTION_ROLES: Final[frozenset[str]] = frozenset({
    "dev_runner",
    "pi_executor",
    "implementation",
    "test",
    "cases",
    "ops",
    "docs",
    "worker",
    "runner",
    "tool_loop",
    "executor",
    "background_review",
})

# Roles protected against lowest-tier ('flash') degradation to preserve long-context fidelity (S3)
COMPRESSION_PROTECTED_ROLES: Final[frozenset[str]] = frozenset({
    "compression",
    "memory",
    "context_compaction",
})

DEFAULT_MAX_PRO_CALLS_PER_TASK: Final[int] = 2

# Machine-verifiable Architectural Decision Records (S5: prevent context loss & arbitrary edits)
ARCHITECTURAL_DECISION_RECORDS: Final[Dict[str, Dict[str, Any]]] = {
    "ADR-S1": {
        "title": "Single Source of Truth for Model Identity & Routing",
        "decision": "All runners (dev_exec_runner, dev_runner, durable_dispatch, ccp_config) must resolve model tier via agentcore.model_routing.",
        "invariant": "No runner may hardcode model='flash' or silently rewrite 'pro' outside enforce_model_tier_budget.",
    },
    "ADR-S2": {
        "title": "Explicit Visibility for Model & Engine Degradation",
        "decision": "Every tier downgrade (pro->main/flash) or engine fallback (hermes->pi) must emit degraded=True and degradation_reason.",
        "invariant": "Completion receipts must surface degradation notices instead of masking quality drops.",
    },
    "ADR-S3": {
        "title": "Long-Context Compaction Floor Protection",
        "decision": "Compression and memory synthesis roles are pinned to >= 'main' tier (medium effort) and excluded from EXECUTION_ROLES.",
        "invariant": "COMPRESSION_PROTECTED_ROLES must remain disjoint from EXECUTION_ROLES and never route to 'flash'.",
    },
    "ADR-S4": {
        "title": "Acyclic Package Boundary Between agentcore and toolsets.pi_executor",
        "decision": "agentcore modules must never import toolsets.pi_executor or hermes_bridge.pi_executor; dependency flows strictly toolsets -> agentcore.",
        "invariant": "AST import graph of agentcore/* contains zero edges to toolsets/* or hermes_bridge/*.",
    },
    "ADR-S5": {
        "title": "Pro Tier Whitelist and Per-Task Quota Guardrail",
        "decision": "Only cognitive/planning/review roles in PRO_ALLOWED_ROLES may use 'pro', capped at DEFAULT_MAX_PRO_CALLS_PER_TASK per task.",
        "invariant": "PRO_ALLOWED_ROLES and EXECUTION_ROLES must be strictly disjoint; quota defaults to 2.",
    },
    "ADR-S6": {
        "title": "Single Completion Card Producer Mutual Exclusion",
        "decision": "When durable-dispatch completion_card_enabled is active, task-progress must yield card emission.",
        "invariant": "At most one plugin emits Feishu completion cards per profile.",
    },
}

# Canonical weakest-to-strongest reasoning effort ladder
REASONING_EFFORT_LADDER: Final[Tuple[str, ...]] = (
    "off",
    "minimal",
    "low",
    "medium",
    "high",
)

# Standard subagent roles for engineering colleagues
MODEL_ROLES: Final[Tuple[str, ...]] = (
    "architect",
    "cases",
    "reviewer",
    "test",
    "triage",
    "research",
    "brain",
    "planning",
    "plan_gate",
    "plan_generator",
    "deep_diagnosis",
    "implementation",
    "design_visual",
    "docs",
    "dev_runner",
    "ops",
    "compression",
    "memory",
    "context_compaction",
)

# Explicit reasoning depth dials
REASONING_DEPTHS: Final[Tuple[str, ...]] = (
    "shallow",
    "standard",
    "deep",
    "ultrabrain",
)

# Task scale dial (independent of role)
TASK_SCALES: Final[Tuple[str, ...]] = ("small", "standard", "large")

MODEL_ROUTE_STATUSES: Final[Tuple[str, ...]] = (
    "routed",
    "choice_required",
    "model_unrouted",
    "model_refused",
    "no_model_catalog",
)

MODEL_ROUTE_PROVENANCES: Final[Tuple[str, ...]] = (
    "request_named_model",
    "request_named_model_unavailable",
    "depth_chain_head",
    "role_chain_head",
    "scale_chain_head",
    "executor_default",
    "fallback",
    "no_catalog",
)

# Role defaults mapping to effort ladder and depth
ROLE_PROFILE_DEFAULTS: Final[Dict[str, Dict[str, str]]] = {
    "architect": {"depth": "ultrabrain", "effort": "high", "tier": "ultrabrain"},
    "brain": {"depth": "ultrabrain", "effort": "high", "tier": "ultrabrain"},
    "planning": {"depth": "ultrabrain", "effort": "high", "tier": "ultrabrain"},
    "plan_gate": {"depth": "ultrabrain", "effort": "high", "tier": "ultrabrain"},
    "plan_generator": {"depth": "ultrabrain", "effort": "high", "tier": "ultrabrain"},
    "deep_diagnosis": {"depth": "ultrabrain", "effort": "high", "tier": "ultrabrain"},
    "reviewer": {"depth": "deep", "effort": "high", "tier": "deep"},
    "triage": {"depth": "deep", "effort": "high", "tier": "deep"},
    "compression": {"depth": "deep", "effort": "medium", "tier": "deep"},
    "memory": {"depth": "deep", "effort": "medium", "tier": "deep"},
    "context_compaction": {"depth": "deep", "effort": "medium", "tier": "deep"},
    "cases": {"depth": "standard", "effort": "medium", "tier": "standard"},
    "implementation": {"depth": "standard", "effort": "medium", "tier": "standard"},
    "research": {"depth": "standard", "effort": "medium", "tier": "standard"},
    "docs": {"depth": "standard", "effort": "low", "tier": "quick"},
    "test": {"depth": "shallow", "effort": "low", "tier": "quick"},
    "dev_runner": {"depth": "shallow", "effort": "low", "tier": "quick"},
    "ops": {"depth": "shallow", "effort": "off", "tier": "quick"},
}

# Standard executor candidate chains
BUILTIN_ROLE_CHAINS: Final[Dict[str, Dict[str, Tuple[Dict[str, str], ...]]]] = {
    "super-agent": {
        "architect": (
            {"model": "pro", "effort": "high"},
            {"model": "main", "effort": "high"},
        ),
        "brain": (
            {"model": "pro", "effort": "high"},
            {"model": "main", "effort": "high"},
        ),
        "planning": (
            {"model": "pro", "effort": "high"},
            {"model": "main", "effort": "high"},
        ),
        "plan_gate": (
            {"model": "pro", "effort": "high"},
            {"model": "main", "effort": "high"},
        ),
        "plan_generator": (
            {"model": "pro", "effort": "high"},
            {"model": "main", "effort": "high"},
        ),
        "deep_diagnosis": (
            {"model": "pro", "effort": "high"},
            {"model": "main", "effort": "high"},
        ),
        "reviewer": (
            {"model": "main", "effort": "high"},
            {"model": "flash", "effort": "medium"},
        ),
        "triage": (
            {"model": "main", "effort": "high"},
            {"model": "flash", "effort": "medium"},
        ),
        "compression": (
            {"model": "main", "effort": "medium"},
        ),
        "memory": (
            {"model": "main", "effort": "medium"},
        ),
        "context_compaction": (
            {"model": "main", "effort": "medium"},
        ),
        "cases": (
            {"model": "flash", "effort": "medium"},
            {"model": "main", "effort": "low"},
        ),
        "implementation": (
            {"model": "flash", "effort": "medium"},
            {"model": "main", "effort": "low"},
        ),
        "research": (
            {"model": "main", "effort": "medium"},
            {"model": "flash", "effort": "low"},
        ),
        "test": (
            {"model": "flash", "effort": "low"},
        ),
        "ops": (
            {"model": "flash", "effort": "off"},
        ),
        "docs": (
            {"model": "flash", "effort": "low"},
        ),
        "dev_runner": (
            {"model": "flash", "effort": "medium"},
        ),
    },
    "hermes": {
        "architect": (
            {"model": "o3-mini", "effort": "high"},
            {"model": "gpt-4o", "effort": "high"},
            {"model": "claude-3-7-sonnet", "effort": "high"},
        ),
        "brain": (
            {"model": "o3-mini", "effort": "high"},
            {"model": "gpt-4o", "effort": "high"},
        ),
        "reviewer": (
            {"model": "o3-mini", "effort": "high"},
            {"model": "gpt-4o", "effort": "medium"},
            {"model": "claude-3-7-sonnet", "effort": "high"},
        ),
        "triage": (
            {"model": "o3-mini", "effort": "high"},
            {"model": "gpt-4o", "effort": "high"},
        ),
        "cases": (
            {"model": "gpt-4o", "effort": "medium"},
            {"model": "o3-mini", "effort": "medium"},
        ),
        "implementation": (
            {"model": "gpt-4o", "effort": "medium"},
            {"model": "o3-mini", "effort": "medium"},
        ),
        "research": (
            {"model": "gpt-4o", "effort": "medium"},
            {"model": "gpt-4o-mini", "effort": "low"},
        ),
        "test": (
            {"model": "gpt-4o-mini", "effort": "low"},
            {"model": "gpt-4o", "effort": "off"},
        ),
        "docs": (
            {"model": "gpt-4o-mini", "effort": "low"},
        ),
    },
    "claude-code": {
        "architect": (
            {"model": "claude-3-7-sonnet", "effort": "high"},
            {"model": "claude-3-5-sonnet", "effort": "high"},
        ),
        "reviewer": (
            {"model": "claude-3-7-sonnet", "effort": "high"},
            {"model": "claude-3-5-sonnet", "effort": "medium"},
        ),
        "triage": (
            {"model": "claude-3-7-sonnet", "effort": "high"},
            {"model": "claude-3-5-sonnet", "effort": "high"},
        ),
        "cases": (
            {"model": "claude-3-5-sonnet", "effort": "medium"},
            {"model": "claude-3-7-sonnet", "effort": "medium"},
        ),
        "test": (
            {"model": "claude-3-5-haiku", "effort": "off"},
        ),
        "research": (
            {"model": "claude-3-5-sonnet", "effort": "medium"},
            {"model": "claude-3-5-haiku", "effort": "low"},
        ),
    },
    "generic": {
        "architect": (
            {"model": "deepseek-reasoner", "effort": "high"},
            {"model": "deepseek-chat", "effort": "high"},
        ),
        "reviewer": (
            {"model": "deepseek-reasoner", "effort": "high"},
            {"model": "deepseek-chat", "effort": "medium"},
        ),
        "triage": (
            {"model": "deepseek-reasoner", "effort": "high"},
            {"model": "deepseek-chat", "effort": "high"},
        ),
        "cases": (
            {"model": "deepseek-chat", "effort": "medium"},
            {"model": "deepseek-reasoner", "effort": "medium"},
        ),
        "test": (
            {"model": "deepseek-chat", "effort": "low"},
        ),
        "research": (
            {"model": "deepseek-chat", "effort": "medium"},
        ),
    },
}

EXECUTOR_DEFAULTS: Final[Dict[str, Dict[str, str]]] = {
    "super-agent": {"model": "main", "effort": "medium"},
    "hermes": {"model": "gpt-4o", "effort": "medium"},
    "claude-code": {"model": "claude-3-5-sonnet", "effort": "medium"},
    "generic": {"model": "deepseek-chat", "effort": "medium"},
}

_NON_GENERATIVE_PATTERNS: Final[Tuple[re.Pattern, ...]] = (
    re.compile(r"^text-embedding-", re.IGNORECASE),
    re.compile(r"^bge-", re.IGNORECASE),
    re.compile(r"^dall-e", re.IGNORECASE),
    re.compile(r"^whisper-", re.IGNORECASE),
    re.compile(r"^tts-", re.IGNORECASE),
)


def is_non_generative(model_name: str) -> bool:
    """Return True if model is an embedding, audio, or visual generator not suited for code/reasoning."""
    name = (model_name or "").strip()
    return any(p.match(name) for p in _NON_GENERATIVE_PATTERNS)


def normalize_effort(effort: str) -> str:
    """Normalize reasoning effort to one of canonical rungs."""
    val = (effort or "").strip().lower()
    if not val:
        return ""
    if val in REASONING_EFFORT_LADDER:
        return val
    if val in ("none", "false", "0"):
        return "off"
    if val in ("min", "minimum"):
        return "minimal"
    if val in ("max", "maximum", "extra_high", "o1_high"):
        return "high"
    return "medium"


def enforce_model_tier_budget(
    model: str,
    *,
    role: str = "",
    task_pro_calls: int = 0,
    max_pro_calls: int = DEFAULT_MAX_PRO_CALLS_PER_TASK,
) -> Tuple[str, Optional[Dict[str, str]]]:
    """Intercept and guard calls to the expensive 'pro' model tier, and protect compression floor.

    Financial & quality circuit breaker rules:
    0. Compression/memory roles (ADR-S3) must never drop to 'flash' -> floor at 'main'.
    1. Execution roles (dev_runner, test, cases, implementation, etc.) are strictly
       prohibited from using 'pro' and are forced to downgrade to 'flash'.
    2. Non-whitelisted roles requesting 'pro' are downgraded to 'main'.
    3. Tasks exceeding the maximum allowed 'pro' quota (default: 2) are downgraded to 'main'.
    """
    clean_model = (model or "").strip()
    clean_role = (role or "").strip().lower()

    if clean_role in COMPRESSION_PROTECTED_ROLES and clean_model.lower() == TIER_FLASH:
        return TIER_MAIN, {
            "stage": "compression_floor_guard",
            "outcome": "upgraded_to_floor",
            "reason": (
                f"[COMPRESSION_GUARD] Role '{clean_role}' is protected from lowest tier "
                f"'{TIER_FLASH}' to prevent long-context pollution (ADR-S3). "
                f"Floor enforced at '{TIER_MAIN}'."
            ),
        }

    if clean_model.lower() != TIER_PRO:
        return clean_model, None

    # Rule 1: Execution roles strictly cannot use pro -> forced downgrade to flash
    if clean_role in EXECUTION_ROLES:
        return TIER_FLASH, {
            "stage": "budget_guard",
            "outcome": "intercepted_and_downgraded",
            "reason": (
                f"[BUDGET_GUARD] Execution role '{clean_role}' is strictly prohibited "
                f"from using expensive tier '{TIER_PRO}'. Forced downgrade to '{TIER_FLASH}' "
                "to prevent financial overdraft."
            ),
        }

    # Rule 2: Unapproved / general roles cannot use pro -> downgrade to main
    if clean_role not in PRO_ALLOWED_ROLES:
        return TIER_MAIN, {
            "stage": "budget_guard",
            "outcome": "intercepted_and_downgraded",
            "reason": (
                f"[BUDGET_GUARD] Role '{clean_role or 'unspecified'}' is not whitelisted "
                f"for '{TIER_PRO}' tier (allowed: {', '.join(sorted(PRO_ALLOWED_ROLES))}). "
                f"Downgraded to '{TIER_MAIN}' to prevent financial overdraft."
            ),
        }

    # Rule 3: Task quota exceeded -> downgrade to main
    if task_pro_calls >= max_pro_calls:
        return TIER_MAIN, {
            "stage": "budget_guard",
            "outcome": "quota_exceeded_downgraded",
            "reason": (
                f"[BUDGET_GUARD] Task exceeded maximum allowed '{TIER_PRO}' calls "
                f"({task_pro_calls} >= {max_pro_calls}). Downgraded to '{TIER_MAIN}' "
                "to prevent runaway billing."
            ),
        }

    return TIER_PRO, {
        "stage": "budget_guard",
        "outcome": "approved",
        "reason": (
            f"[BUDGET_GUARD] Role '{clean_role}' approved for '{TIER_PRO}' tier "
            f"({task_pro_calls + 1}/{max_pro_calls} calls used)."
        ),
    }


def _extract_degradation(attempted: Sequence[Mapping[str, str]], *, fallback_used: bool = False) -> Tuple[bool, str]:
    """Derive explicit degradation status and reason from route attempt records (ADR-S2)."""
    reasons: List[str] = []
    for rec in attempted:
        outcome = rec.get("outcome", "")
        if outcome in ("intercepted_and_downgraded", "quota_exceeded_downgraded"):
            reasons.append(rec.get("reason", outcome))
        elif rec.get("stage") == "role_chain" and outcome == "candidate_unavailable":
            reasons.append(rec.get("reason", "primary role candidate unavailable"))
    if fallback_used and not reasons:
        for rec in attempted:
            if rec.get("stage") == "role_chain" and rec.get("outcome") == "unavailable":
                reasons.append(rec.get("reason", "role chain unavailable; used executor default"))
    return (bool(reasons), "; ".join(reasons))


def resolve_model_route(
    executor_profile: str,
    *,
    requested_model: str = "",
    requested_effort: str = "",
    role: str = "",
    requested_depth: str = "",
    requested_scale: str = "",
    task_pro_calls: int = 0,
    max_pro_calls: int = DEFAULT_MAX_PRO_CALLS_PER_TASK,
    chains: Optional[Mapping[str, Mapping[str, Sequence[Mapping[str, str]]]]] = None,
    available_models: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Pure-function deterministic model and reasoning effort resolution with budget guardrails.

    Precedence:
    1. requested_model (explicit request subject to budget guardrail and non_generative filter)
    2. requested_depth / requested_role candidate chain
    3. executor default fallback
    """
    profile = (executor_profile or "super-agent").strip().lower()
    if profile not in BUILTIN_ROLE_CHAINS and profile not in EXECUTOR_DEFAULTS:
        profile = "generic"

    clean_role = (role or "").strip().lower()
    clean_depth = (requested_depth or "").strip().lower()
    clean_scale = (requested_scale or "").strip().lower()
    clean_effort = normalize_effort(requested_effort)

    attempted: List[Dict[str, str]] = []
    available_set = set(m.lower() for m in available_models) if available_models is not None else None

    # Step 1: Explicit requested model check
    if requested_model.strip():
        req_model = requested_model.strip()
        if is_non_generative(req_model):
            attempted.append({
                "stage": "request_model",
                "outcome": "refused",
                "reason": f"Model `{req_model}` is non-generative",
            })
            return {
                "schema_version": CODING_MODEL_ROUTE_SCHEMA_VERSION,
                "status": "model_refused",
                "selected_model": "",
                "selected_reasoning_effort": "",
                "selected_tier": "quick",
                "role": clean_role,
                "depth": clean_depth,
                "scale": clean_scale,
                "provenance": "request_named_model",
                "degraded": True,
                "degradation_reason": f"Model `{req_model}` is non-generative",
                "attempted": attempted,
            }

        # Budget Guardrail Check for explicit model request
        guarded_model, guard_record = enforce_model_tier_budget(
            req_model,
            role=clean_role,
            task_pro_calls=task_pro_calls,
            max_pro_calls=max_pro_calls,
        )
        if guard_record:
            attempted.append(guard_record)
        req_model = guarded_model

        effective_effort = clean_effort or (
            "high" if req_model == TIER_PRO else ("medium" if req_model == TIER_MAIN else "low")
        )
        attempted.append({
            "stage": "request_model",
            "outcome": "applied",
            "reason": f"Applied model `{req_model}`",
        })
        selected_tier = (
            "ultrabrain"
            if req_model == TIER_PRO or effective_effort == "high"
            else ("deep" if req_model == TIER_MAIN or effective_effort == "medium" else "quick")
        )
        degraded, degradation_reason = _extract_degradation(attempted)
        return {
            "schema_version": CODING_MODEL_ROUTE_SCHEMA_VERSION,
            "status": "routed",
            "selected_model": req_model,
            "selected_reasoning_effort": effective_effort,
            "selected_tier": selected_tier,
            "role": clean_role,
            "depth": clean_depth,
            "scale": clean_scale,
            "provenance": "request_named_model",
            "degraded": degraded,
            "degradation_reason": degradation_reason,
            "attempted": attempted,
        }

    # Step 2: Role and Depth based chain resolution
    chain_table = chains.get(profile, {}) if chains and profile in chains else BUILTIN_ROLE_CHAINS.get(profile, {})
    role_chain: Sequence[Mapping[str, str]] = ()
    provenance = "executor_default"

    # Dial depth escalation/saving
    role_defaults = ROLE_PROFILE_DEFAULTS.get(clean_role, {})
    effective_depth = clean_depth or role_defaults.get("depth", "standard")
    tier = role_defaults.get("tier", "standard")

    if clean_role and clean_role in chain_table:
        role_chain = chain_table[clean_role]
        provenance = "role_chain_head"
        attempted.append({
            "stage": "role_chain",
            "outcome": "found",
            "reason": f"Found candidate chain for role `{clean_role}` on profile `{profile}`",
        })
    elif clean_role:
        attempted.append({
            "stage": "role_chain",
            "outcome": "unavailable",
            "reason": f"No candidate chain for role `{clean_role}` on profile `{profile}`",
        })

    # Filter candidate chain against available_models and budget guardrail
    selected_candidate: Optional[Mapping[str, str]] = None
    for idx, cand in enumerate(role_chain):
        cand_model = cand.get("model", "")
        guarded_cand, guard_rec = enforce_model_tier_budget(
            cand_model,
            role=clean_role,
            task_pro_calls=task_pro_calls,
            max_pro_calls=max_pro_calls,
        )
        if guard_rec:
            attempted.append(guard_rec)

        if available_set is None or guarded_cand.lower() in available_set:
            selected_candidate = dict(cand)
            selected_candidate["model"] = guarded_cand
            break
        attempted.append({
            "stage": "role_chain",
            "outcome": "candidate_unavailable",
            "reason": f"Candidate `{guarded_cand}` (index {idx}) unavailable in catalog",
        })

    if selected_candidate:
        cand_model = selected_candidate.get("model", "")
        eff = clean_effort or selected_candidate.get("effort", "medium")
        if cand_model == TIER_PRO or effective_depth == "ultrabrain":
            eff = "high"
            tier = "ultrabrain"
        elif cand_model == TIER_MAIN or (effective_depth == "deep" and eff in ("off", "low")):
            eff = "medium" if eff in ("off", "low") else eff
            tier = "deep"
        elif cand_model == TIER_FLASH or effective_depth == "shallow":
            eff = "low" if eff in ("high", "medium") else eff
            tier = "quick"

        degraded, degradation_reason = _extract_degradation(attempted)
        return {
            "schema_version": CODING_MODEL_ROUTE_SCHEMA_VERSION,
            "status": "routed",
            "selected_model": cand_model,
            "selected_reasoning_effort": eff,
            "selected_tier": tier,
            "role": clean_role,
            "depth": effective_depth,
            "scale": clean_scale,
            "provenance": provenance,
            "degraded": degraded,
            "degradation_reason": degradation_reason,
            "attempted": attempted,
        }

    # Step 3: Executor default fallback
    default_entry = EXECUTOR_DEFAULTS.get(profile, EXECUTOR_DEFAULTS["generic"])
    default_model = default_entry.get("model", "")
    guarded_default, guard_rec = enforce_model_tier_budget(
        default_model,
        role=clean_role,
        task_pro_calls=task_pro_calls,
        max_pro_calls=max_pro_calls,
    )
    if guard_rec:
        attempted.append(guard_rec)

    attempted.append({
        "stage": "executor_default",
        "outcome": "applied",
        "reason": f"Fell back to default configuration for profile `{profile}`",
    })

    eff = clean_effort or default_entry.get("effort", "medium")
    if guarded_default == TIER_PRO or effective_depth == "ultrabrain":
        eff = "high"
        tier = "ultrabrain"
    elif guarded_default == TIER_MAIN:
        eff = eff or "medium"
        tier = "deep"
    elif guarded_default == TIER_FLASH or effective_depth == "shallow":
        eff = "low"
        tier = "quick"

    degraded, degradation_reason = _extract_degradation(attempted, fallback_used=bool(clean_role))
    return {
        "schema_version": CODING_MODEL_ROUTE_SCHEMA_VERSION,
        "status": "routed",
        "selected_model": guarded_default,
        "selected_reasoning_effort": eff,
        "selected_tier": tier,
        "role": clean_role,
        "depth": effective_depth,
        "scale": clean_scale,
        "provenance": "executor_default",
        "degraded": degraded,
        "degradation_reason": degradation_reason,
        "attempted": attempted,
    }


def resolve_colleague_model(
    role: str,
    requested_model: str = "",
    *,
    task_pro_calls: int = 0,
    max_pro_calls: int = DEFAULT_MAX_PRO_CALLS_PER_TASK,
) -> Dict[str, Any]:
    """Canonical single-source-of-truth model resolver for colleagues and workers (ADR-S1)."""
    return resolve_model_route(
        "super-agent",
        role=role,
        requested_model=requested_model,
        task_pro_calls=task_pro_calls,
        max_pro_calls=max_pro_calls,
    )

