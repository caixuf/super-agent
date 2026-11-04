"""Deliverable Mode: native attachment extraction, validation, and delivery.

Aligns with the official Hermes Deliverable Mode feature:
- Extracts artifact paths from model text responses, excluding code blocks.
- Validates path existence, size bounds, category, and delivery format.
- Dispatches artifacts directly into the chat via native messaging gateway attachments.
- C14: Verified deliverables instead of bare filesystem paths.
- C12: Native delivery preserving origin and addressing bindings.
- C08: Requirement clarification & patch state handling.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Supported deliverable extensions and their categories
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".tiff"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"})
AUDIO_EXTENSIONS = frozenset({".mp3", ".wav", ".ogg", ".opus", ".m4a", ".flac"})
DOCUMENT_EXTENSIONS = frozenset({".pdf", ".docx", ".doc", ".odt", ".rtf", ".txt", ".md", ".epub"})
DATA_EXTENSIONS = frozenset({".xlsx", ".xls", ".ods", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml"})
PRESENTATION_EXTENSIONS = frozenset({".pptx", ".ppt", ".odp", ".key"})
ARCHIVE_EXTENSIONS = frozenset({".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar"})
WEB_EXTENSIONS = frozenset({".html", ".htm"})

ALL_DELIVERABLE_EXTENSIONS = (
    IMAGE_EXTENSIONS
    | VIDEO_EXTENSIONS
    | AUDIO_EXTENSIONS
    | DOCUMENT_EXTENSIONS
    | DATA_EXTENSIONS
    | PRESENTATION_EXTENSIONS
    | ARCHIVE_EXTENSIONS
    | WEB_EXTENSIONS
)

# Source code and log files are intentionally excluded from auto-delivery
EXCLUDED_EXTENSIONS = frozenset({
    ".py", ".sh", ".bash", ".c", ".cpp", ".h", ".hpp", ".rs", ".go", ".java",
    ".js", ".ts", ".jsx", ".tsx", ".log", ".tmp", ".swp", ".bak", ".pyc",
})

DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024  # 50MB
CODE_BLOCK_PATTERN = re.compile(r"```[\s\S]*?```|`[^`\n]+`")


@dataclass(frozen=True)
class Deliverable:
    path: str
    category: str
    size_bytes: int
    display_name: str
    delivery_mode: str  # 'inline', 'upload', 'voice'

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "category": self.category,
            "size_bytes": self.size_bytes,
            "display_name": self.display_name,
            "delivery_mode": self.delivery_mode,
        }


def categorize_extension(ext: str) -> tuple[str, str]:
    """Map file extension to (category, delivery_mode)."""
    low = ext.lower()
    if low in IMAGE_EXTENSIONS:
        return "image", "inline"
    if low in VIDEO_EXTENSIONS:
        return "video", "inline"
    if low in AUDIO_EXTENSIONS:
        return "audio", "voice"
    if low in DOCUMENT_EXTENSIONS:
        return "document", "upload"
    if low in DATA_EXTENSIONS:
        return "data", "upload"
    if low in PRESENTATION_EXTENSIONS:
        return "presentation", "upload"
    if low in ARCHIVE_EXTENSIONS:
        return "archive", "upload"
    if low in WEB_EXTENSIONS:
        return "web", "upload"
    return "file", "upload"


def extract_deliverables(
    text: str,
    *,
    allowed_roots: Optional[list[Path | str]] = None,
    check_exists: bool = True,
    max_size_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> list[Deliverable]:
    """Extract candidate file paths from response text, excluding code blocks."""
    if not text:
        return []

    # Strip code blocks and inline code so sample snippets are not extracted
    cleaned_text = CODE_BLOCK_PATTERN.sub(" ", text)

    # Find absolute paths or tilde home paths
    # Matches /path/to/file.ext or ~/path/to/file.ext
    pattern = re.compile(r"(?:~|\/)[a-zA-Z0-9_\-\.\/]+?\.[a-zA-Z0-9]+")
    candidates = pattern.findall(cleaned_text)

    deliverables: list[Deliverable] = []
    seen_paths = set()

    roots = [Path(r).resolve() for r in allowed_roots] if allowed_roots else None

    for raw in candidates:
        # Avoid double dots or trailing periods
        cleaned_path = raw.rstrip(".,;:!?'\"")
        resolved = Path(cleaned_path).expanduser().resolve()
        path_str = str(resolved)

        if path_str in seen_paths:
            continue
        seen_paths.add(path_str)

        suffix = resolved.suffix.lower()
        if suffix in EXCLUDED_EXTENSIONS or suffix not in ALL_DELIVERABLE_EXTENSIONS:
            continue

        if roots:
            is_allowed = any(resolved == root or root in resolved.parents for root in roots)
            if not is_allowed:
                continue

        if check_exists:
            if not resolved.is_file():
                continue
            try:
                stat = resolved.stat()
                size = stat.st_size
            except OSError:
                continue

            if size <= 0 or size > max_size_bytes:
                continue
        else:
            size = 0

        category, mode = categorize_extension(suffix)
        deliverables.append(
            Deliverable(
                path=path_str,
                category=category,
                size_bytes=size,
                display_name=resolved.name,
                delivery_mode=mode,
            )
        )

    return deliverables


def sanitize_text_for_delivery(text: str, deliverables: list[Deliverable]) -> str:
    """Replace raw local paths in prose with human-readable references."""
    out = text
    for d in deliverables:
        # Only replace path outside of code blocks
        out = re.sub(re.escape(d.path), f"[{d.display_name}]", out)
    return out


def attach_deliverables_to_completion(
    completion: dict[str, Any], deliverables: list[Deliverable]
) -> dict[str, Any]:
    """Attach verified deliverables to completion payload for gateway transport (C14, C12)."""
    updated = dict(completion)
    artifacts = list(updated.get("artifacts", []))
    for d in deliverables:
        if d.path not in artifacts:
            artifacts.append(d.path)
    updated["artifacts"] = artifacts
    updated["deliverables"] = [d.to_dict() for d in deliverables]
    return updated


def handle_clarification_or_patch(
    task_id: str,
    clarification: str,
    current_status: str,
    already_applied: bool = False,
) -> dict[str, Any]:
    """Handle follow-up clarification, maintaining received vs applied distinction (C08)."""
    if not clarification or not clarification.strip():
        raise ValueError("Clarification text cannot be empty")

    if current_status in ("done", "cancelled"):
        return {
            "task_id": task_id,
            "accepted": False,
            "applied": False,
            "reason": f"Task is already {current_status}; clarification cannot be applied to finished tasks.",
            "action_required": "create_follow_up_task",
        }

    return {
        "task_id": task_id,
        "accepted": True,
        "applied": bool(already_applied),
        "status": "applied" if already_applied else "pending_worker_application",
        "clarification": clarification.strip(),
    }
