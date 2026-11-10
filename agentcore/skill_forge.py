"""agentcore.skill_forge — Markdown-native Procedural Skill Forge & Case Memory.

Ported and adapted from EverOS (EverMind-AI/EverOS) dual-track memory architecture:
- User Track focuses on profiles, entities, and conversation episodes.
- Agent Track focuses on operational cases (AgentCase) and distilled reusable procedures (ProceduralSkill).

Features:
- Markdown Source of Truth: skills stored as readable, diffable .md files with YAML frontmatter.
- Case Ledger: records task intent, approach, quality score, verified files, and key insights.
- Skill Distillation & Lifecycle: tracks confidence and maturity_score (0.0 to 1.0) through execution validation.
- Hybrid Intent Recall: ranks procedural skills by intent keywords, domain matching, and maturity level.
- Prompt Guidance Renderer: formats matching procedural skills for direct context injection.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Union
import uuid


_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "into", "when", "then",
    "how", "what", "why", "who", "are", "was", "were", "will", "can", "should",
    "任务", "执行", "进行", "一个", "我们", "需要", "以及", "通过", "使用", "完成",
})


def _extract_skill_tokens(text: str) -> List[str]:
    """Extract normalized alphanumeric tokens and CJK 2-grams/phrases for skill matching."""
    if not text:
        return []
    lowered = text.lower()
    tokens: List[str] = []
    seen: set[str] = set()

    def _add(tok: str) -> None:
        t = tok.strip("._-")
        if len(t) >= 2 and t not in _STOPWORDS and t not in seen:
            seen.add(t)
            tokens.append(t)

    for m in re.finditer(r"[a-z0-9_.\-]+", lowered):
        word = m.group(0)
        _add(word)
        if "_" in word or "-" in word or "." in word:
            for part in re.split(r"[_\-.]+", word):
                _add(part)

    for m in re.finditer(r"[\u4e00-\u9fff]+", lowered):
        chunk = m.group(0)
        n = len(chunk)
        if 2 <= n <= 6:
            _add(chunk)
        for i in range(n - 1):
            _add(chunk[i : i + 2])

    return tokens


def _synthesize_skill_content(
    cases: Sequence["AgentCase"],
    *,
    domain: str = "default",
) -> tuple[str, str, str]:
    """Auto-synthesize (name, description, procedure_markdown) from a list of AgentCase records."""
    primary = cases[0]
    role_label = (primary.role or domain or "default").strip()
    raw_intent = re.sub(r"\s+", " ", (primary.task_intent or "").strip())
    short_intent = raw_intent[:42] if raw_intent else f"{role_label} standard procedure"
    name = f"[{domain}/{role_label}] {short_intent}"

    intents = []
    insights = []
    approaches = []
    verified: List[str] = []
    for c in cases:
        ci = re.sub(r"\s+", " ", (c.task_intent or "").strip())
        if ci and ci not in intents:
            intents.append(ci[:160])
        ca = re.sub(r"\s+", " ", (c.approach or "").strip())
        if ca and ca not in approaches:
            approaches.append(ca[:360])
        ck = re.sub(r"\s+", " ", (c.key_insight or "").strip())
        if ck and ck not in insights:
            insights.append(ck[:200])
        for vf in c.verified_files:
            vf_name = Path(str(vf)).name if vf else ""
            if vf_name and vf_name not in verified:
                verified.append(vf_name)

    description = f"从 {len(cases)} 个真实执行案例自动蒸馏的标准作业流程，适用场景：{'；'.join(intents[:3])}"
    step_lines = [
        f"1. **目标识别与上下文对齐**：确认任务意图匹配 `{short_intent}`（领域：`{domain}`，角色：`{role_label}`）。",
    ]
    if approaches:
        for idx, app in enumerate(approaches[:3], start=2):
            step_lines.append(f"{idx}. **核心执行方案 ({idx - 1})**：{app}")
    else:
        step_lines.append("2. **核心执行方案**：遵循最小改动与确定性验证原则执行目标操作。")

    next_idx = len(step_lines) + 1
    if insights:
        step_lines.append(f"{next_idx}. **关键工程洞察**：{'；'.join(insights[:3])}")
        next_idx += 1
    if verified:
        step_lines.append(f"{next_idx}. **产物与门禁核验**：确保生成并核验关键产物文件（`{', '.join(verified[:6])}`）。")

    procedure_markdown = "\n".join(step_lines)
    return name, description, procedure_markdown


@dataclass
class AgentCase:
    """An execution trace and outcome record of an agent/colleague task."""

    case_id: str
    task_intent: str
    approach: str
    quality_score: float = 80.0
    key_insight: Optional[str] = None
    domain: str = "default"
    role: str = "cases"
    verified_files: List[str] = field(default_factory=list)
    approved: bool = False
    verification_receipt_id: Optional[str] = None
    profile: str = "default"
    subject: str = "default"
    team: str = "default"
    timestamp: int = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> AgentCase:
        return cls(
            case_id=str(data.get("case_id", uuid.uuid4().hex[:12])),
            task_intent=str(data.get("task_intent", "")),
            approach=str(data.get("approach", "")),
            quality_score=float(data.get("quality_score", 80.0)),
            key_insight=data.get("key_insight"),
            domain=str(data.get("domain", "default")),
            role=str(data.get("role", "cases")),
            verified_files=list(data.get("verified_files", [])),
            approved=bool(data.get("approved", False)),
            verification_receipt_id=data.get("verification_receipt_id"),
            profile=str(data.get("profile", "default")),
            subject=str(data.get("subject", "default")),
            team=str(data.get("team", "default")),
            timestamp=int(data.get("timestamp", int(time.time()))),
        )


@dataclass
class ProceduralSkill:
    """A reusable procedural skill aggregated and distilled from successful cases."""

    skill_id: str
    name: str
    description: str
    procedure_markdown: str
    domain: str = "default"
    trigger_patterns: List[str] = field(default_factory=list)
    confidence: float = 0.8
    maturity_score: float = 0.5
    source_case_ids: List[str] = field(default_factory=list)
    approved: bool = False
    revoked: bool = False
    profile: str = "default"
    subject: str = "default"
    team: str = "default"
    created_at: int = field(default_factory=lambda: int(time.time()))
    updated_at: int = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ProceduralSkill:
        return cls(
            skill_id=str(data.get("skill_id", uuid.uuid4().hex[:12])),
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            procedure_markdown=str(data.get("procedure_markdown", "")),
            domain=str(data.get("domain", "default")),
            trigger_patterns=list(data.get("trigger_patterns", [])),
            confidence=float(data.get("confidence", 0.8)),
            maturity_score=float(data.get("maturity_score", 0.5)),
            source_case_ids=list(data.get("source_case_ids", [])),
            approved=bool(data.get("approved", False)),
            revoked=bool(data.get("revoked", False)),
            profile=str(data.get("profile", "default")),
            subject=str(data.get("subject", "default")),
            team=str(data.get("team", "default")),
            created_at=int(data.get("created_at", int(time.time()))),
            updated_at=int(data.get("updated_at", int(time.time()))),
        )

    def to_markdown(self) -> str:
        """Serialize skill as canonical Markdown with YAML-like frontmatter."""
        triggers_json = json.dumps(self.trigger_patterns, ensure_ascii=False)
        cases_json = json.dumps(self.source_case_ids, ensure_ascii=False)
        return f"""---
skill_id: {self.skill_id}
name: {self.name}
domain: {self.domain}
profile: {self.profile}
subject: {self.subject}
team: {self.team}
approved: {'true' if self.approved else 'false'}
revoked: {'true' if self.revoked else 'false'}
confidence: {self.confidence:.2f}
maturity_score: {self.maturity_score:.2f}
triggers: {triggers_json}
source_cases: {cases_json}
created_at: {self.created_at}
updated_at: {self.updated_at}
---

# {self.name}

## 简要描述
{self.description}

## 标准操作步骤与工程规范
{self.procedure_markdown}
"""

    @classmethod
    def from_markdown(cls, text: str) -> ProceduralSkill:
        """Parse skill from canonical Markdown."""
        frontmatter_match = re.match(r"^---\n(.*?)\n---\n(.*)$", text.strip(), re.DOTALL)
        if not frontmatter_match:
            raise ValueError("invalid_skill_markdown: missing frontmatter delimiter")
        fm_text, body_text = frontmatter_match.group(1), frontmatter_match.group(2)

        meta: Dict[str, Any] = {}
        for line in fm_text.splitlines():
            if ":" in line:
                key, val = line.split(":", 1)
                k = key.strip()
                v = val.strip()
                if v.startswith("[") and v.endswith("]"):
                    try:
                        meta[k] = json.loads(v)
                    except Exception:
                        meta[k] = []
                else:
                    meta[k] = v

        desc = ""
        desc_match = re.search(r"## 简要描述\n(.*?)(?=\n## |\Z)", body_text, re.DOTALL)
        if desc_match:
            desc = desc_match.group(1).strip()

        proc = ""
        proc_match = re.search(r"## 标准操作步骤与工程规范\n(.*?)(?=\n## |\Z)", body_text, re.DOTALL)
        if proc_match:
            proc = proc_match.group(1).strip()
        elif not desc:
            proc = body_text.strip()

        approved_val = meta.get("approved", "false")
        if isinstance(approved_val, bool):
            approved = approved_val
        else:
            approved = str(approved_val).strip().lower() in ("true", "1", "yes")

        revoked_val = meta.get("revoked", "false")
        if isinstance(revoked_val, bool):
            revoked = revoked_val
        else:
            revoked = str(revoked_val).strip().lower() in ("true", "1", "yes")

        profile = str(meta.get("profile", "default"))
        subject = str(meta.get("subject", "default"))
        team = str(meta.get("team", "default"))

        return cls(
            skill_id=meta.get("skill_id", uuid.uuid4().hex[:12]),
            name=meta.get("name", "Untitled Skill"),
            description=desc or meta.get("name", ""),
            procedure_markdown=proc or body_text.strip(),
            domain=meta.get("domain", "default"),
            trigger_patterns=meta.get("triggers", []),
            confidence=float(meta.get("confidence", 0.8)),
            maturity_score=float(meta.get("maturity_score", 0.5)),
            source_case_ids=meta.get("source_cases", []),
            approved=approved,
            revoked=revoked,
            profile=profile,
            subject=subject,
            team=team,
            created_at=int(meta.get("created_at", int(time.time()))),
            updated_at=int(meta.get("updated_at", int(time.time()))),
        )


class SkillForge:
    """EverOS-style local-first Procedural Skill Forge & Case Memory manager.

    Maintains canonical Markdown procedural skills and structured execution cases.
    Provides keyword & intent hybrid recall, maturity scoring, and prompt guidance.
    """

    def __init__(self, storage_dir: Optional[Union[str, Path]] = None, in_memory: bool = False):
        self.in_memory = in_memory
        if storage_dir is None:
            env_dir = (os.environ.get("SUPERAGENT_SKILL_FORGE_DIR") or "").strip()
            if env_dir:
                self.storage_dir = Path(env_dir).expanduser().resolve()
            else:
                self.storage_dir = Path.home() / ".hermes" / "state" / "skill-forge"
        else:
            self.storage_dir = Path(storage_dir).resolve()

        self._skills: Dict[str, ProceduralSkill] = {}
        self._cases: Dict[str, AgentCase] = {}

        if not self.in_memory:
            self.storage_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            (self.storage_dir / "skills").mkdir(parents=True, exist_ok=True, mode=0o700)
            (self.storage_dir / "cases").mkdir(parents=True, exist_ok=True, mode=0o700)
            self.sync_from_disk()

    def record_case(self, case: AgentCase) -> str:
        """Store an execution case into the memory ledger."""
        self._cases[case.case_id] = case
        if not self.in_memory:
            case_path = self.storage_dir / "cases" / f"{case.case_id}.json"
            tmp_path = case_path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(case.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp_path, case_path)
        return case.case_id

    def get_case(
        self,
        case_id: str,
        *,
        profile: Optional[str] = None,
        subject: Optional[str] = None,
        team: Optional[str] = None,
        allow_unapproved: bool = True,
    ) -> Optional[AgentCase]:
        case = self._cases.get(case_id)
        if not case:
            return None
        if not case.approved and not allow_unapproved:
            raise PermissionError(f"Access denied: case '{case_id}' is a candidate and has not been approved")
        if case.profile != "default" and (profile is None or (profile != "*" and profile != case.profile)):
            raise PermissionError(f"Cross-profile access denied: case profile '{case.profile}' != requested '{profile}'")
        if case.subject != "default" and (subject is None or (subject != "*" and subject != case.subject)):
            raise PermissionError(f"Cross-subject access denied: case subject '{case.subject}' != requested '{subject}'")
        if case.team != "default" and (team is None or (team != "*" and team != case.team)):
            raise PermissionError(f"Cross-team access denied: case team '{case.team}' != requested '{team}'")
        return case

    def approve_case(self, case_id: str, *, verification_receipt_id: Optional[str] = None) -> AgentCase:
        """Promote an execution case to approved status after verifying real evidence."""
        case = self._cases.get(case_id)
        if not case:
            raise KeyError(f"Case '{case_id}' not found")
        receipt_id = verification_receipt_id or case.verification_receipt_id
        if not receipt_id and not case.verified_files:
            raise ValueError(f"Cannot approve case '{case_id}': missing real verification evidence (verification_receipt_id or verified_files)")
        if receipt_id:
            case.verification_receipt_id = receipt_id
        case.approved = True
        if not self.in_memory:
            case_path = self.storage_dir / "cases" / f"{case.case_id}.json"
            tmp_path = case_path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(case.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp_path, case_path)
        return case

    def list_cases(
        self,
        domain: Optional[str] = None,
        *,
        only_approved: bool = False,
        profile: Optional[str] = None,
        subject: Optional[str] = None,
        team: Optional[str] = None,
    ) -> List[AgentCase]:
        cases = list(self._cases.values())
        if domain:
            cases = [c for c in cases if c.domain == domain]
        if only_approved:
            cases = [c for c in cases if c.approved]
        if profile and profile != "*":
            cases = [c for c in cases if c.profile in (profile, "default")]
        elif profile is None:
            cases = [c for c in cases if c.profile == "default"]
        if subject and subject != "*":
            cases = [c for c in cases if c.subject in (subject, "default")]
        elif subject is None:
            cases = [c for c in cases if c.subject == "default"]
        if team and team != "*":
            cases = [c for c in cases if c.team in (team, "default")]
        elif team is None:
            cases = [c for c in cases if c.team == "default"]
        return sorted(cases, key=lambda c: c.timestamp, reverse=True)

    def register_skill(self, skill: ProceduralSkill) -> None:
        """Register or update a procedural skill."""
        self._skills[skill.skill_id] = skill
        if not self.in_memory:
            domain_dir = self.storage_dir / "skills" / skill.domain
            domain_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            skill_file = domain_dir / f"{skill.skill_id}.md"
            tmp_file = skill_file.with_suffix(".tmp")
            tmp_file.write_text(skill.to_markdown(), encoding="utf-8")
            os.replace(tmp_file, skill_file)

    def distill_skill_from_cases(
        self,
        cases: Sequence[AgentCase],
        name: Optional[str] = None,
        description: Optional[str] = None,
        procedure_markdown: Optional[str] = None,
        domain: str = "default",
        trigger_patterns: Optional[List[str]] = None,
        profile: str = "default",
        subject: str = "default",
        team: str = "default",
        approved: bool = False,
        skill_id: Optional[str] = None,
    ) -> ProceduralSkill:
        """Distill a collection of successful cases into an authoritative ProceduralSkill.

        When name, description, or procedure_markdown are omitted, automatically synthesizes
        canonical Markdown skill content from the supplied AgentCase records.
        """
        if not cases:
            raise ValueError("cannot_distill_from_empty_cases")

        if approved:
            for c in cases:
                if not c.approved:
                    raise ValueError(f"Cannot distill approved skill: source case '{c.case_id}' is not approved")
                if not c.verification_receipt_id and not c.verified_files:
                    raise ValueError(f"Cannot distill approved skill: source case '{c.case_id}' lacks verification evidence")

        auto_name, auto_desc, auto_proc = _synthesize_skill_content(cases, domain=domain)
        final_name = (name or "").strip() or auto_name
        final_desc = (description or "").strip() or auto_desc
        final_proc = (procedure_markdown or "").strip() or auto_proc

        case_ids = [c.case_id for c in cases]
        avg_score = sum(c.quality_score for c in cases) / len(cases)
        # Higher score and more case corroboration yields higher initial maturity
        maturity = min(0.95, (avg_score / 100.0) * (0.5 + 0.1 * min(len(cases), 5)))

        triggers = list(trigger_patterns or [])
        if not triggers:
            for c in cases:
                for token in _extract_skill_tokens(c.task_intent + " " + (c.approach or "")):
                    if token not in triggers:
                        triggers.append(token)
            triggers = triggers[:12]

        resolved_skill_id = skill_id or f"skill_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        skill = ProceduralSkill(
            skill_id=resolved_skill_id,
            name=final_name,
            description=final_desc,
            procedure_markdown=final_proc,
            domain=domain,
            trigger_patterns=triggers,
            confidence=round(min(1.0, avg_score / 100.0), 2),
            maturity_score=round(maturity, 2),
            source_case_ids=case_ids,
            approved=approved,
            revoked=False,
            profile=profile,
            subject=subject,
            team=team,
        )
        self.register_skill(skill)
        return skill

    def auto_distill_from_cases(
        self,
        *,
        domain: Optional[str] = None,
        role: Optional[str] = None,
        min_cases: int = 1,
        min_quality: float = 80.0,
    ) -> List[ProceduralSkill]:
        """Automatically distill or update ProceduralSkill Markdown files from recorded AgentCases."""
        eligible: List[AgentCase] = []
        for c in self._cases.values():
            if domain and c.domain != domain:
                continue
            if role and c.role != role:
                continue
            if c.quality_score < min_quality:
                continue
            if not (c.task_intent or "").strip() or not (c.approach or "").strip():
                continue
            eligible.append(c)

        if not eligible:
            return []

        # Group cases by (domain, role, profile, subject, team) and cluster by primary intent token
        groups: Dict[tuple[str, str, str, str, str, str], List[AgentCase]] = {}
        for c in sorted(eligible, key=lambda x: x.timestamp, reverse=True):
            toks = _extract_skill_tokens(c.task_intent)
            topic_key = toks[0] if toks else (c.role or "general")
            gkey = (c.domain or "default", c.role or "cases", c.profile, c.subject, c.team, topic_key)
            groups.setdefault(gkey, []).append(c)

        distilled: List[ProceduralSkill] = []
        for (g_domain, g_role, g_profile, g_subject, g_team, topic_key), group_cases in groups.items():
            if len(group_cases) < min_cases:
                continue
            safe_topic = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fff]+", "_", topic_key).strip("_")[:24] or "general"
            det_skill_id = f"skill_auto_{g_domain}_{g_role}_{safe_topic}"
            existing = self._skills.get(det_skill_id)
            group_ids = [c.case_id for c in group_cases]
            if existing and set(group_ids).issubset(set(existing.source_case_ids)):
                # Already up to date on disk; ensure file exists
                if not self.in_memory:
                    skill_file = self.storage_dir / "skills" / existing.domain / f"{existing.skill_id}.md"
                    if not skill_file.is_file():
                        self.register_skill(existing)
                distilled.append(existing)
                continue

            all_approved = all(
                c.approved and bool(c.verification_receipt_id or c.verified_files)
                for c in group_cases
            )
            skill = self.distill_skill_from_cases(
                cases=group_cases,
                domain=g_domain,
                profile=g_profile,
                subject=g_subject,
                team=g_team,
                approved=all_approved,
                skill_id=det_skill_id,
            )
            distilled.append(skill)

        return distilled

    def approve_skill(self, skill_id: str) -> ProceduralSkill:
        """Approve a procedural skill for production usage."""
        skill = self._skills.get(skill_id)
        if not skill:
            raise KeyError(f"Skill '{skill_id}' not found")
        for cid in skill.source_case_ids:
            case = self._cases.get(cid)
            if case:
                if not case.approved:
                    raise ValueError(f"Cannot approve skill '{skill_id}': source case '{cid}' is unapproved")
                if not case.verification_receipt_id and not case.verified_files:
                    raise ValueError(f"Cannot approve skill '{skill_id}': source case '{cid}' lacks verification evidence")
        skill.approved = True
        skill.revoked = False
        skill.updated_at = int(time.time())
        self.register_skill(skill)
        return skill

    def revoke_skill(self, skill_id: str, reason: str = "") -> ProceduralSkill:
        """Revoke a procedural skill, immediately denying access to production guidance."""
        skill = self._skills.get(skill_id)
        if not skill:
            raise KeyError(f"Skill '{skill_id}' not found")
        skill.revoked = True
        skill.approved = False
        skill.updated_at = int(time.time())
        self.register_skill(skill)
        return skill

    def promote_skill(self, skill_id: str, delta: float = 0.1) -> Optional[ProceduralSkill]:
        """Boost skill maturity score after a successful application in production."""
        skill = self._skills.get(skill_id)
        if not skill:
            return None
        new_maturity = min(1.0, skill.maturity_score + delta)
        skill.maturity_score = round(new_maturity, 2)
        skill.updated_at = int(time.time())
        self.register_skill(skill)
        return skill

    def get_skill(
        self,
        skill_id: str,
        *,
        profile: Optional[str] = None,
        subject: Optional[str] = None,
        team: Optional[str] = None,
        allow_revoked: bool = False,
        allow_unapproved: bool = True,
    ) -> Optional[ProceduralSkill]:
        skill = self._skills.get(skill_id)
        if not skill:
            return None
        if skill.revoked and not allow_revoked:
            raise PermissionError(f"Access denied: skill '{skill_id}' has been revoked")
        if not skill.approved and not allow_unapproved:
            raise PermissionError(f"Access denied: skill '{skill_id}' is a candidate and has not been approved")
        if skill.profile != "default" and (profile is None or (profile != "*" and profile != skill.profile)):
            raise PermissionError(f"Cross-profile access denied: skill profile '{skill.profile}' != requested '{profile}'")
        if skill.subject != "default" and (subject is None or (subject != "*" and subject != skill.subject)):
            raise PermissionError(f"Cross-subject access denied: skill subject '{skill.subject}' != requested '{subject}'")
        if skill.team != "default" and (team is None or (team != "*" and team != skill.team)):
            raise PermissionError(f"Cross-team access denied: skill team '{skill.team}' != requested '{team}'")
        return skill

    def list_skills(
        self,
        domain: Optional[str] = None,
        *,
        include_unapproved: bool = True,
        include_revoked: bool = False,
        profile: Optional[str] = None,
        subject: Optional[str] = None,
        team: Optional[str] = None,
    ) -> List[ProceduralSkill]:
        skills = list(self._skills.values())
        if domain:
            skills = [s for s in skills if s.domain == domain]
        if not include_revoked:
            skills = [s for s in skills if not s.revoked]
        if not include_unapproved:
            skills = [s for s in skills if s.approved]
        if profile and profile != "*":
            skills = [s for s in skills if s.profile in (profile, "default")]
        elif profile is None:
            skills = [s for s in skills if s.profile == "default"]
        if subject and subject != "*":
            skills = [s for s in skills if s.subject in (subject, "default")]
        elif subject is None:
            skills = [s for s in skills if s.subject == "default"]
        if team and team != "*":
            skills = [s for s in skills if s.team in (team, "default")]
        elif team is None:
            skills = [s for s in skills if s.team == "default"]
        return sorted(skills, key=lambda s: s.maturity_score, reverse=True)

    def recall_skills(
        self,
        query: str,
        domain: Optional[str] = None,
        top_k: int = 3,
        min_maturity: float = 0.0,
        *,
        only_approved: bool = False,
        include_unapproved: Optional[bool] = None,
        include_revoked: bool = False,
        profile: Optional[str] = None,
        subject: Optional[str] = None,
        team: Optional[str] = None,
    ) -> List[ProceduralSkill]:
        """Hybrid intent recall combining trigger matching, textual similarity, and maturity score."""
        q_lower = (query or "").lower()
        q_tokens = set(_extract_skill_tokens(query)) | set(re.findall(r"[\w]+", q_lower))
        scored: List[tuple[float, ProceduralSkill]] = []

        allow_unapproved = not only_approved if include_unapproved is None else include_unapproved

        for skill in self._skills.values():
            if domain and skill.domain != domain and skill.domain != "default":
                continue
            if skill.maturity_score < min_maturity:
                continue
            if skill.revoked and not include_revoked:
                continue
            if not skill.approved and not allow_unapproved:
                continue
            if skill.profile != "default" and (profile is None or (profile != "*" and profile != skill.profile)):
                continue
            if skill.subject != "default" and (subject is None or (subject != "*" and subject != skill.subject)):
                continue
            if skill.team != "default" and (team is None or (team != "*" and team != skill.team)):
                continue

            # 1. Trigger patterns match (highest weight)
            trigger_hits = sum(
                1 for t in skill.trigger_patterns
                if t and (t.lower() in q_lower or t.lower() in q_tokens)
            )

            # 2. Name & description token overlap
            name_tokens = set(_extract_skill_tokens(skill.name)) | set(re.findall(r"[\w]+", skill.name.lower()))
            desc_tokens = set(_extract_skill_tokens(skill.description)) | set(re.findall(r"[\w]+", skill.description.lower()))
            overlap = len(q_tokens.intersection(name_tokens | desc_tokens))

            if trigger_hits == 0 and overlap == 0:
                continue

            # Composite ranking score: triggers (0.5) + text overlap (0.2) + maturity (0.3)
            score = (trigger_hits * 0.5) + (overlap * 0.2) + (skill.maturity_score * 0.3)
            if score > 0.1:
                scored.append((score, skill))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [skill for _, skill in scored[:top_k]]

    def render_guidance(
        self,
        query: str,
        domain: Optional[str] = None,
        top_k: int = 2,
        *,
        only_approved: bool = False,
        include_unapproved: Optional[bool] = None,
        include_revoked: bool = False,
        profile: Optional[str] = None,
        subject: Optional[str] = None,
        team: Optional[str] = None,
    ) -> str:
        """Format recalled procedural skills as prompt guidance."""
        skills = self.recall_skills(
            query,
            domain=domain,
            top_k=top_k,
            only_approved=only_approved,
            include_unapproved=include_unapproved,
            include_revoked=include_revoked,
            profile=profile,
            subject=subject,
            team=team,
        )
        if not skills:
            return ""

        lines = ["## 经过工程检验的先例技能 (Verified Procedural Skills)"]
        for s in skills:
            status_tag = "" if s.approved else " [CANDIDATE]"
            lines.append(f"\n### 先例：{s.name}{status_tag} (成熟度: {s.maturity_score:.2f})")
            lines.append(f"**核心要领**：{s.description}")
            lines.append(f"**推荐操作规程**：\n{s.procedure_markdown}\n")
        return "\n".join(lines)

    def render_production_guidance(
        self,
        query: str,
        domain: Optional[str] = None,
        top_k: int = 2,
        *,
        profile: Optional[str] = None,
        subject: Optional[str] = None,
        team: Optional[str] = None,
    ) -> str:
        """Format recalled procedural skills strictly for production (unapproved and revoked excluded)."""
        return self.render_guidance(
            query,
            domain=domain,
            top_k=top_k,
            only_approved=True,
            include_unapproved=False,
            include_revoked=False,
            profile=profile,
            subject=subject,
            team=team,
        )

    def sync_from_disk(self) -> int:
        """Cascade-style local disk synchronization from canonical Markdown and JSON files."""
        count = 0
        skills_root = self.storage_dir / "skills"
        if skills_root.is_dir():
            for md_path in skills_root.rglob("*.md"):
                try:
                    text = md_path.read_text(encoding="utf-8")
                    skill = ProceduralSkill.from_markdown(text)
                    self._skills[skill.skill_id] = skill
                    count += 1
                except Exception:
                    continue

        cases_root = self.storage_dir / "cases"
        if cases_root.is_dir():
            for json_path in cases_root.glob("*.json"):
                try:
                    data = json.loads(json_path.read_text(encoding="utf-8"))
                    case = AgentCase.from_dict(data)
                    self._cases[case.case_id] = case
                except Exception:
                    continue

        return count

