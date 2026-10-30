"""agentcore.memory_hygiene — Long-term memory auditing, compaction, and governance.

Enforces the 70% golden capacity threshold (<14,000 characters against 20,000 character budget)
to prevent context dilution, drift, and bloating. Detects duplicate facts via bigram Jaccard
similarity and prunes expired temporary scratch sections while preserving durable system rules.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
from typing import Any, Dict, List, Optional, Set, Tuple, Union

DEFAULT_MAX_CHARACTERS = 20000
WARN_THRESHOLD_RATIO = 0.70
CRITICAL_THRESHOLD_RATIO = 0.85

_TEMPORARY_SECTION_PATTERNS = [
    re.compile(r"^##?\s*(临时|待办|草稿|调试|scratch|temporary|debug)", re.IGNORECASE),
    re.compile(r"^\s*\[(临时|草稿|scratch|debug)\]", re.IGNORECASE),
]


def _bigrams(text: str) -> Set[str]:
    cleaned = re.sub(r"\s+", "", text.strip())
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i:i + 2] for i in range(len(cleaned) - 1)}


def bigram_jaccard(s1: str, s2: str) -> float:
    """Calculate character-level bigram Jaccard similarity."""
    b1, b2 = _bigrams(s1), _bigrams(s2)
    if not b1 and not b2:
        return 1.0
    if not b1 or not b2:
        return 0.0
    intersection = len(b1 & b2)
    union = len(b1 | b2)
    return intersection / union if union > 0 else 0.0


class MemoryHygieneAuditor:
    """Audits and compacts long-term agent memory stores."""

    def __init__(self, max_characters: int = DEFAULT_MAX_CHARACTERS):
        self.max_characters = max_characters

    def split_sections(self, text: str) -> List[str]:
        """Split memory text by section delimiter '§' or top-level markdown headers."""
        if "§" in text:
            raw_sections = [s.strip() for s in text.split("§")]
            return [s for s in raw_sections if s]
        lines = text.splitlines()
        sections: List[List[str]] = []
        current: List[str] = []
        for line in lines:
            if line.startswith("# ") or line.startswith("## "):
                if current:
                    sections.append(current)
                current = [line]
            else:
                current.append(line)
        if current:
            sections.append(current)
        return ["\n".join(sec).strip() for sec in sections if any(line.strip() for line in sec)]

    def is_temporary_section(self, section: str) -> bool:
        """Check whether a section contains temporary or ephemeral scratch data."""
        first_line = section.splitlines()[0].strip() if section else ""
        return any(pat.search(first_line) for pat in _TEMPORARY_SECTION_PATTERNS)

    def find_duplicates(self, sections: List[str], threshold: float = 0.85) -> List[Dict[str, Any]]:
        """Identify redundant duplicate sections using bigram Jaccard similarity."""
        duplicates: List[Dict[str, Any]] = []
        n = len(sections)
        for i in range(n):
            for j in range(i + 1, n):
                sim = bigram_jaccard(sections[i], sections[j])
                if sim >= threshold:
                    duplicates.append({
                        "index_a": i,
                        "index_b": j,
                        "similarity": round(sim, 3),
                        "snippet_a": sections[i][:80],
                        "snippet_b": sections[j][:80],
                    })
        return duplicates

    def audit(self, text: str) -> Dict[str, Any]:
        """Perform a comprehensive hygiene audit on the given memory text."""
        char_count = len(text)
        ratio = char_count / self.max_characters if self.max_characters > 0 else 1.0
        percent = round(ratio * 100.0, 1)

        if ratio >= CRITICAL_THRESHOLD_RATIO:
            status = "critical"
        elif ratio >= WARN_THRESHOLD_RATIO:
            status = "warning"
        else:
            status = "healthy"

        sections = self.split_sections(text)
        temp_indices = [i for i, s in enumerate(sections) if self.is_temporary_section(s)]
        duplicates = self.find_duplicates(sections)

        return {
            "ok": True,
            "character_count": char_count,
            "max_characters": self.max_characters,
            "usage_percent": percent,
            "status": status,
            "healthy": status == "healthy",
            "section_count": len(sections),
            "temporary_sections_count": len(temp_indices),
            "duplicate_pairs_count": len(duplicates),
            "duplicate_pairs": duplicates,
            "warning": status != "healthy",
        }

    def compact(
        self,
        text: str,
        prune_temporary: bool = True,
        deduplicate: bool = True,
        similarity_threshold: float = 0.85,
    ) -> Tuple[str, Dict[str, Any]]:
        """Compact memory text by removing temporary sections and redundant duplicates."""
        before_audit = self.audit(text)
        sections = self.split_sections(text)
        kept_sections: List[str] = []
        pruned_temporary = 0
        pruned_duplicates = 0

        for sec in sections:
            if prune_temporary and self.is_temporary_section(sec):
                pruned_temporary += 1
                continue
            if deduplicate and any(bigram_jaccard(sec, existing) >= similarity_threshold for existing in kept_sections):
                pruned_duplicates += 1
                continue
            kept_sections.append(sec)

        separator = "\n§\n" if "§" in text else "\n\n"
        compacted_text = separator.join(kept_sections).strip() + "\n"
        after_audit = self.audit(compacted_text)

        summary = {
            "characters_before": before_audit["character_count"],
            "characters_after": after_audit["character_count"],
            "saved_characters": before_audit["character_count"] - after_audit["character_count"],
            "pruned_temporary_sections": pruned_temporary,
            "pruned_duplicate_sections": pruned_duplicates,
            "status_before": before_audit["status"],
            "status_after": after_audit["status"],
        }
        return compacted_text, summary


def audit_memory_file(file_path: Union[str, Path], max_characters: int = DEFAULT_MAX_CHARACTERS) -> Dict[str, Any]:
    """Audit a memory markdown file."""
    path = Path(file_path).expanduser().resolve()
    if not path.is_file():
        return {"ok": False, "error": f"file_not_found: {path}"}
    content = path.read_text(encoding="utf-8")
    auditor = MemoryHygieneAuditor(max_characters=max_characters)
    res = auditor.audit(content)
    res["file"] = str(path)
    return res


def compact_memory_file(
    file_path: Union[str, Path],
    backup: bool = True,
    max_characters: int = DEFAULT_MAX_CHARACTERS,
) -> Dict[str, Any]:
    """Compact a memory markdown file and optionally save a backup."""
    path = Path(file_path).expanduser().resolve()
    if not path.is_file():
        return {"ok": False, "error": f"file_not_found: {path}"}
    content = path.read_text(encoding="utf-8")
    auditor = MemoryHygieneAuditor(max_characters=max_characters)
    compacted, summary = auditor.compact(content)

    if backup:
        backup_path = path.with_suffix(f".bak.{int(time.time())}")
        shutil.copy2(path, backup_path)
        summary["backup_file"] = str(backup_path)

    path.write_text(compacted, encoding="utf-8")
    summary["ok"] = True
    summary["file"] = str(path)
    return summary
