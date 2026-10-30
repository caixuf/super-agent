"""Subagent multi-agent coordination protocol, boundary confinement, and instruction fidelity.

Ported and adapted from open-source multi-agent fanout and delegation protocols:
- Enforces strict boundary confinement between subagent units (overlap & forbidden touch detection).
- Preserves instruction fidelity via Goal Echo, Completion Criteria, Verification Stop, and Search Discipline.
- Validates structured return envelopes (subagent_result/v1) preventing hallucinated completions.
- Provides cascading failure containment to halt or fence downstream tasks when errors occur.
"""
from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
from typing import Any, Final, Mapping, Sequence

SUBAGENT_PROMPT_CONTRACT_SCHEMA: Final[str] = "subagent_prompt_contract/v1"
SUBAGENT_RESULT_SCHEMA: Final[str] = "subagent_result/v1"
SUBAGENT_BOUNDARY_SCHEMA: Final[str] = "subagent_boundary_manifest/v1"

MAX_SUBAGENT_UNITS: Final[int] = 16
MAX_COMPLETION_CRITERIA: Final[int] = 10
SUBAGENT_STATUSES: Final[tuple[str, ...]] = (
    "completed",
    "blocked",
    "needs_replan",
    "failed",
)

_UNIT_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")

GOAL_ECHO_PROTOCOL: Final[str] = (
    "## Instruction Fidelity Protocol 1: Goal Echo-Back\n"
    "Before invoking any mutating tool (write, edit, bash), you MUST clearly state:\n"
    "1. Restated Goal: What specific deliverable is expected.\n"
    "2. Scope & Boundaries: Which files you may edit and which are forbidden (do_not_touch).\n"
    "3. Completion Criteria: How success is verified.\n"
    "If you detect an instruction contradiction or boundary overlap, STOP and report immediately."
)

VERIFICATION_STOP_PROTOCOL: Final[str] = (
    "## Instruction Fidelity Protocol 2: Verification & Stop Discipline\n"
    "- Mandatory Verification: You must execute at least one verification pass for your changes.\n"
    "- Bounded Fix Cycles: Maximum 2 fix-and-verify attempts. If tests still fail after 2 cycles, "
    "stop and report the exact blocker rather than spinning in loops.\n"
    "- Boundary vs Bug: A permission denial, sandbox restriction, or policy rejection is a "
    "boundary constraint, NOT a bug to bypass."
)

SEARCH_DISCIPLINE_PROTOCOL: Final[str] = (
    "## Instruction Fidelity Protocol 3: Capped Structural Search\n"
    "Bound file exploration to targeted queries (symbol search or narrow path match) before "
    "reading entire files. Stop searching as soon as the target definition is located."
)


@dataclass(frozen=True)
class SubagentUnit:
    unit_id: str
    title: str
    goal: str
    target_paths: tuple[str, ...] = ()
    do_not_touch: tuple[str, ...] = ()
    criteria: tuple[str, ...] = ()
    role_name: str = "worker"
    role_contract: str = ""

    def __post_init__(self) -> None:
        if not _UNIT_ID_RE.fullmatch(self.unit_id):
            raise ValueError(f"invalid unit_id: {self.unit_id!r}")
        if not self.goal.strip():
            raise ValueError("subagent unit goal cannot be empty")
        if len(self.criteria) > MAX_COMPLETION_CRITERIA:
            raise ValueError(f"criteria count ({len(self.criteria)}) exceeds limit {MAX_COMPLETION_CRITERIA}")
        if not isinstance(self.target_paths, tuple):
            object.__setattr__(self, "target_paths", tuple(self.target_paths))
        if not isinstance(self.do_not_touch, tuple):
            object.__setattr__(self, "do_not_touch", tuple(self.do_not_touch))
        if not isinstance(self.criteria, tuple):
            object.__setattr__(self, "criteria", tuple(self.criteria))


def _normalize_path(p: str) -> str:
    cleaned = p.strip().replace("\\", "/")
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    cleaned = posixpath.normpath(cleaned).rstrip("/")
    if cleaned == ".":
        return "."
    return cleaned.lstrip("/")


def _paths_conflict(p1: str, p2: str) -> bool:
    norm1 = _normalize_path(p1)
    norm2 = _normalize_path(p2)
    if not norm1 or not norm2:
        return False
    if norm1 == norm2:
        return True
    if norm1 == "." or norm2 == ".":
        return True
    if fnmatch.fnmatch(norm1, norm2) or fnmatch.fnmatch(norm2, norm1):
        return True
    try:
        path1 = PurePosixPath(norm1)
        path2 = PurePosixPath(norm2)
        if path1 in path2.parents or path2 in path1.parents:
            return True
    except Exception:
        pass
    return False


def detect_boundary_conflicts(
    units: Sequence[SubagentUnit | Mapping[str, Any]],
    *,
    protected_paths: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Detect boundary overlaps, self-contradictions, and protected path violations."""
    if len(units) > MAX_SUBAGENT_UNITS:
        raise ValueError(f"unit count ({len(units)}) exceeds limit {MAX_SUBAGENT_UNITS}")

    normalized: list[SubagentUnit] = []
    for u in units:
        if isinstance(u, SubagentUnit):
            normalized.append(u)
        else:
            normalized.append(
                SubagentUnit(
                    unit_id=str(u.get("unit_id", "")).strip(),
                    title=str(u.get("title", "")).strip(),
                    goal=str(u.get("goal", "")).strip(),
                    target_paths=tuple(str(x) for x in u.get("target_paths", ())),
                    do_not_touch=tuple(str(x) for x in u.get("do_not_touch", ())),
                    criteria=tuple(str(x) for x in u.get("criteria", ())),
                    role_name=str(u.get("role_name", "worker")),
                    role_contract=str(u.get("role_contract", "")),
                )
            )

    conflicts: list[dict[str, Any]] = []

    # Check 1: Per-unit self-contradiction & protected paths
    for u in normalized:
        self_hits: set[str] = set()
        for t in u.target_paths:
            for d in u.do_not_touch:
                if _paths_conflict(t, d):
                    self_hits.add(_normalize_path(t))
        if self_hits:
            conflicts.append({
                "conflict_type": "self_contradiction",
                "unit_id": u.unit_id,
                "paths": sorted(self_hits),
                "description": f"Unit {u.unit_id} targets paths conflicting with do_not_touch: {sorted(self_hits)}",
            })

        prot_hits: set[str] = set()
        for t in u.target_paths:
            for p in protected_paths:
                if _paths_conflict(t, p):
                    prot_hits.add(_normalize_path(t))
        if prot_hits:
            conflicts.append({
                "conflict_type": "protected_path_violation",
                "unit_id": u.unit_id,
                "paths": sorted(prot_hits),
                "description": f"Unit {u.unit_id} targets globally protected paths: {sorted(prot_hits)}",
            })

    # Check 2: Inter-unit write overlap
    for i in range(len(normalized)):
        u1 = normalized[i]
        for j in range(i + 1, len(normalized)):
            u2 = normalized[j]
            overlap_hits: set[str] = set()
            for t1 in u1.target_paths:
                for t2 in u2.target_paths:
                    if _paths_conflict(t1, t2):
                        overlap_hits.add(_normalize_path(t1))
                        overlap_hits.add(_normalize_path(t2))
            if overlap_hits:
                conflicts.append({
                    "conflict_type": "write_overlap",
                    "units": [u1.unit_id, u2.unit_id],
                    "paths": sorted(overlap_hits),
                    "description": (
                        f"Units {u1.unit_id} and {u2.unit_id} both declare conflicting target modification "
                        f"paths: {sorted(overlap_hits)}"
                    ),
                })

    return conflicts


def assemble_subagent_prompt(
    overall_goal: str,
    unit: SubagentUnit | Mapping[str, Any],
    *,
    shared_preamble: bool = True,
) -> str:
    """Assemble a deterministic, prompt-cache friendly prompt for a subagent unit."""
    if not isinstance(unit, SubagentUnit):
        unit = SubagentUnit(
            unit_id=str(unit.get("unit_id", "")).strip(),
            title=str(unit.get("title", "")).strip(),
            goal=str(unit.get("goal", "")).strip(),
            target_paths=tuple(str(x) for x in unit.get("target_paths", ())),
            do_not_touch=tuple(str(x) for x in unit.get("do_not_touch", ())),
            criteria=tuple(str(x) for x in unit.get("criteria", ())),
            role_name=str(unit.get("role_name", "worker")),
            role_contract=str(unit.get("role_contract", "")),
        )

    sections: list[str] = []

    # Zone 1: Shared Preamble (identical across sibling units for prefix caching)
    if shared_preamble:
        sections.append(f"# Global Goal\n{overall_goal.strip()}\n")
        sections.append(GOAL_ECHO_PROTOCOL)
        sections.append(VERIFICATION_STOP_PROTOCOL)
        sections.append(SEARCH_DISCIPLINE_PROTOCOL)

    # Zone 2: Unit Boundary & Scope
    scope_lines = [
        f"## Subagent Assignment: {unit.unit_id} ({unit.title or 'Unit Task'})",
        f"**Sub-Goal**: {unit.goal}",
    ]
    if unit.target_paths:
        scope_lines.append(f"**Allowed Target Paths**: {', '.join(unit.target_paths)}")
    if unit.do_not_touch:
        scope_lines.append(f"**DO NOT TOUCH (Forbidden)**: {', '.join(unit.do_not_touch)}")
    if unit.criteria:
        scope_lines.append("**Completion Criteria**:")
        for idx, crit in enumerate(unit.criteria, 1):
            scope_lines.append(f"{idx}. {crit}")

    sections.append("\n".join(scope_lines))

    # Zone 3: Role Instructions
    if unit.role_contract.strip():
        sections.append(f"## Role Instructions ({unit.role_name})\n{unit.role_contract.strip()}")

    # Zone 4: Structured Return Envelope
    tail = (
        "## Terminal Output Contract\n"
        "When finished, your final message MUST end with a JSON envelope adhering to `subagent_result/v1`:\n"
        "```json\n"
        "{\n"
        f'  "schema_version": "{SUBAGENT_RESULT_SCHEMA}",\n'
        f'  "unit_id": "{unit.unit_id}",\n'
        '  "status": "completed | blocked | needs_replan | failed",\n'
        '  "summary": "Concise summary of work performed",\n'
        '  "deliverables": ["file1", "file2"],\n'
        '  "criteria_met": [true, true],\n'
        '  "verification_evidence": "Description of verification ran and exit status"\n'
        "}\n"
        "```"
    )
    sections.append(tail)

    return "\n\n".join(sections)


def validate_subagent_result(raw: str | Mapping[str, Any]) -> tuple[bool, dict[str, Any], list[str]]:
    """Extract and validate the subagent result envelope from raw text or mapping."""
    errors: list[str] = []
    data: dict[str, Any] = {}

    if isinstance(raw, Mapping):
        data = dict(raw)
    elif isinstance(raw, str):
        data_parsed = None
        # Strategy 1: Check code fences with json or without, scanning backwards
        fence_pattern = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
        matches = list(fence_pattern.finditer(raw))
        for m in reversed(matches):
            try:
                candidate = json.loads(m.group(1).strip())
                if isinstance(candidate, dict) and candidate.get("schema_version") == SUBAGENT_RESULT_SCHEMA:
                    data_parsed = candidate
                    break
            except Exception:
                continue

        if data_parsed is None:
            for m in reversed(matches):
                try:
                    candidate = json.loads(m.group(1).strip())
                    if isinstance(candidate, dict):
                        data_parsed = candidate
                        break
                except Exception:
                    continue

        if data_parsed is None:
            # Strategy 2: Scan unfenced text for balanced braces { ... } from tail
            brace_starts = [i for i, c in enumerate(raw) if c == "{"]
            for start in reversed(brace_starts):
                depth = 0
                in_string = False
                escape = False
                end = -1
                for i in range(start, len(raw)):
                    char = raw[i]
                    if escape:
                        escape = False
                        continue
                    if char == "\\":
                        escape = True
                        continue
                    if char == '"':
                        in_string = not in_string
                        continue
                    if not in_string:
                        if char == "{":
                            depth += 1
                        elif char == "}":
                            depth -= 1
                            if depth == 0:
                                end = i
                                break
                if end != -1:
                    try:
                        candidate = json.loads(raw[start : end + 1])
                        if isinstance(candidate, dict):
                            if candidate.get("schema_version") == SUBAGENT_RESULT_SCHEMA:
                                data_parsed = candidate
                                break
                            if data_parsed is None:
                                data_parsed = candidate
                    except Exception:
                        continue

        if data_parsed is None:
            # Fallback: simple first '{' to last '}'
            start = raw.find("{")
            end = raw.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    candidate = json.loads(raw[start : end + 1])
                    if isinstance(candidate, dict):
                        data_parsed = candidate
                    else:
                        return False, {}, ["parsed JSON is not an object"]
                except Exception as exc:
                    return False, {}, [f"JSON parse error: {exc}"]
            else:
                return False, {}, ["no JSON object found in output"]

        data = data_parsed or {}

    # Schema verification
    if data.get("schema_version") != SUBAGENT_RESULT_SCHEMA:
        errors.append(f"schema_version must be {SUBAGENT_RESULT_SCHEMA}")
    if not data.get("unit_id"):
        errors.append("unit_id is required")
    status = data.get("status")
    if status not in SUBAGENT_STATUSES:
        errors.append(f"status must be one of {SUBAGENT_STATUSES}")
    if not data.get("summary"):
        errors.append("summary is required")

    criteria_met = data.get("criteria_met")
    if status == "completed":
        if not isinstance(criteria_met, (list, tuple)) or not criteria_met:
            errors.append("criteria_met must be a non-empty sequence for completed status")
        elif not all(isinstance(c, bool) and c is True for c in criteria_met):
            errors.append("all criteria_met entries must be true for completed status")

    return (len(errors) == 0), data, errors


def contain_cascading_failure(
    failed_unit_id: str,
    failure_decision: Mapping[str, Any],
    all_unit_ids: Sequence[str],
) -> dict[str, Any]:
    """Contain error blast radius and decide fail-closed policy for sibling/downstream units."""
    action = failure_decision.get("action", "escalate")
    kind = failure_decision.get("kind", "unknown")

    siblings = [uid for uid in all_unit_ids if uid != failed_unit_id]

    if action == "stop" or kind == "permanent":
        # Fail closed immediately: boundary or permanent policy violation halts workflow
        return {
            "containment_action": "halt_all",
            "fail_closed": True,
            "failed_unit": failed_unit_id,
            "halted_siblings": siblings,
            "reason": f"permanent failure in unit {failed_unit_id} halts workflow to prevent corrupted state",
        }
    elif action == "replan" or kind == "persistent":
        # Worktree conflict or state mismatch requires replanning the batch
        return {
            "containment_action": "pause_for_replan",
            "fail_closed": True,
            "failed_unit": failed_unit_id,
            "halted_siblings": siblings,
            "reason": f"concurrency or state conflict in unit {failed_unit_id} requires batch replan",
        }
    elif action == "retry":
        # Transient failure isolated to this unit; siblings may continue
        return {
            "containment_action": "isolate_unit_retry",
            "fail_closed": False,
            "failed_unit": failed_unit_id,
            "halted_siblings": [],
            "reason": f"transient failure in unit {failed_unit_id} allows retry within local unit quota",
        }
    else:
        # Unknown/escalation requires operator attention; pause execution
        return {
            "containment_action": "escalate_batch",
            "fail_closed": True,
            "failed_unit": failed_unit_id,
            "halted_siblings": siblings,
            "reason": f"unresolved failure in unit {failed_unit_id} escalated for operator review",
        }
