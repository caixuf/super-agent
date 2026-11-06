"""Delivery Sanitizer and Artifact Extractor for ChatOps Deliverables.

Guards against leaking internal server absolute paths (e.g. /home/<user>/...)
to user chats (IM platforms). Automatically resolves and extracts referenced
output artifacts (HTML reports, images, archives) so they can be delivered as
first-class chat attachments (file/image keys) instead of dead local paths.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

# Pattern matching absolute filesystem paths in assistant outputs (both files and directories)
PATH_PATTERN = re.compile(
    r'(?<![a-zA-Z0-9_\-\./])(/(?:home|data|tmp|root|opt|var)(?:/[a-zA-Z0-9_\-\.]+)+)'
)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif"}
DOC_FILE_EXTS = {".html", ".htm", ".pdf", ".zip", ".tar.gz", ".csv", ".json", ".xlsx", ".docx", ".log", ".txt"}


def sanitize_and_extract_artifacts(
    text: str,
    *,
    base_dir: Optional[str] = None,
    allow_nonexistent: bool = False
) -> Tuple[str, List[Dict[str, Any]]]:
    """Sanitize absolute paths in `text` and extract deliverable artifacts.

    Returns:
        (sanitized_text, list_of_artifact_dicts)
        where each artifact dict has:
          {"kind": "image"|"file", "path": str, "name": str}
    """
    if not isinstance(text, str) or not text:
        return text, []

    artifacts: List[Dict[str, Any]] = []
    seen_paths = set()

    def _replace_path(match: re.Match) -> str:
        raw_path = match.group(1)
        p = Path(raw_path).resolve()

        # Handle directory
        if p.exists() and p.is_dir():
            parts = p.parts
            if len(parts) >= 3 and parts[1] == "home":
                return ".../" + "/".join(parts[3:]) if len(parts) > 3 else p.name
            return ".../" + p.name

        # Check file existence
        exists = p.exists() and p.is_file()
        if not exists and not allow_nonexistent:
            # If path does not exist, sanitize out server-specific home and system paths
            parts = p.parts
            if len(parts) >= 3 and parts[1] == "home":
                return ".../" + "/".join(parts[3:]) if len(parts) > 3 else p.name
            return ".../" + "/".join(parts[2:]) if len(parts) > 2 else p.name

        ext = p.suffix.lower()
        if ext in IMAGE_EXTS:
            if str(p) not in seen_paths:
                artifacts.append({"kind": "image", "path": str(p), "name": p.name})
                seen_paths.add(str(p))
            return f"📷 [图片附件已随附上传: {p.name}]"
        elif ext in DOC_FILE_EXTS:
            if str(p) not in seen_paths:
                artifacts.append({"kind": "file", "path": str(p), "name": p.name})
                seen_paths.add(str(p))
            return f"📎 [文件附件已随附上传: {p.name}]"
        else:
            # Code files or unknown extensions: sanitize server home
            parts = p.parts
            if len(parts) >= 3 and parts[1] == "home":
                return ".../" + "/".join(parts[3:]) if len(parts) > 3 else p.name
            return ".../" + p.name

    # Perform replacement
    sanitized = PATH_PATTERN.sub(_replace_path, text)
    return sanitized, artifacts
