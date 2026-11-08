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
    approved: bool = True
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
            approved=bool(data.get("approved", True)),
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
            base = Path.home() / ".hermes" / "state" / "skill-forge"
            self.storage_dir = base
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
            case_path.write_text(json.dumps(case.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        return case.case_id

    def get_case(self, case_id: str) -> Optional[AgentCase]:
        return self._cases.get(case_id)

    def list_cases(self, domain: Optional[str] = None) -> List[AgentCase]:
        cases = list(self._cases.values())
        if domain:
            cases = [c for c in cases if c.domain == domain]
        return sorted(cases, key=lambda c: c.timestamp, reverse=True)

    def register_skill(self, skill: ProceduralSkill) -> None:
        """Register or update a procedural skill."""
        self._skills[skill.skill_id] = skill
        if not self.in_memory:
            domain_dir = self.storage_dir / "skills" / skill.domain
            domain_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            skill_file = domain_dir / f"{skill.skill_id}.md"
            skill_file.write_text(skill.to_markdown(), encoding="utf-8")

    def distill_skill_from_cases(
        self,
        cases: Sequence[AgentCase],
        name: str,
        description: str,
        procedure_markdown: str,
        domain: str = "default",
        trigger_patterns: Optional[List[str]] = None,
    ) -> ProceduralSkill:
        """Distill a collection of successful cases into an authoritative ProceduralSkill."""
        if not cases:
            raise ValueError("cannot_distill_from_empty_cases")

        case_ids = [c.case_id for c in cases]
        avg_score = sum(c.quality_score for c in cases) / len(cases)
        # Higher score and more case corroboration yields higher initial maturity
        maturity = min(0.95, (avg_score / 100.0) * (0.5 + 0.1 * min(len(cases), 5)))

        triggers = list(trigger_patterns or [])
        if not triggers:
            # Extract common terms from intents
            for c in cases:
                for token in re.findall(r"[\w]+", c.task_intent.lower()):
                    if len(token) >= 2 and token not in triggers:
                        triggers.append(token)
            triggers = triggers[:8]

        skill_id = f"skill_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        skill = ProceduralSkill(
            skill_id=skill_id,
            name=name,
            description=description,
            procedure_markdown=procedure_markdown,
            domain=domain,
            trigger_patterns=triggers,
            confidence=round(min(1.0, avg_score / 100.0), 2),
            maturity_score=round(maturity, 2),
            source_case_ids=case_ids,
        )
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

    def get_skill(self, skill_id: str) -> Optional[ProceduralSkill]:
        return self._skills.get(skill_id)

    def list_skills(self, domain: Optional[str] = None) -> List[ProceduralSkill]:
        skills = list(self._skills.values())
        if domain:
            skills = [s for s in skills if s.domain == domain]
        return sorted(skills, key=lambda s: s.maturity_score, reverse=True)

    def recall_skills(
        self,
        query: str,
        domain: Optional[str] = None,
        top_k: int = 3,
        min_maturity: float = 0.0,
    ) -> List[ProceduralSkill]:
        """Hybrid intent recall combining trigger matching, textual similarity, and maturity score."""
        q_tokens = set(re.findall(r"[\w]+", query.lower()))
        scored: List[tuple[float, ProceduralSkill]] = []

        for skill in self._skills.values():
            if domain and skill.domain != domain and skill.domain != "default":
                continue
            if skill.maturity_score < min_maturity:
                continue

            # 1. Trigger patterns match (highest weight)
            trigger_hits = sum(1 for t in skill.trigger_patterns if t.lower() in query.lower() or t.lower() in q_tokens)

            # 2. Name & description token overlap
            name_tokens = set(re.findall(r"[\w]+", skill.name.lower()))
            desc_tokens = set(re.findall(r"[\w]+", skill.description.lower()))
            overlap = len(q_tokens.intersection(name_tokens | desc_tokens))

            # Composite ranking score: triggers (0.5) + text overlap (0.2) + maturity (0.3)
            score = (trigger_hits * 0.5) + (overlap * 0.2) + (skill.maturity_score * 0.3)
            if score > 0.1:
                scored.append((score, skill))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [skill for _, skill in scored[:top_k]]

    def render_guidance(self, query: str, domain: Optional[str] = None, top_k: int = 2) -> str:
        """Format recalled procedural skills as prompt guidance."""
        skills = self.recall_skills(query, domain=domain, top_k=top_k)
        if not skills:
            return ""

        lines = ["## 经过工程检验的先例技能 (Verified Procedural Skills)"]
        for s in skills:
            lines.append(f"\n### 先例：{s.name} (成熟度: {s.maturity_score:.2f})")
            lines.append(f"**核心要领**：{s.description}")
            lines.append(f"**推荐操作规程**：\n{s.procedure_markdown}\n")
        return "\n".join(lines)

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

