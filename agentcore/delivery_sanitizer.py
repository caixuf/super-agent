"""Delivery Sanitizer and Artifact Extractor for ChatOps Deliverables.

Guards against leaking internal server absolute paths (e.g. /home/<user>/...)
to user chats (IM platforms). Automatically resolves and extracts referenced
output artifacts (HTML reports, images, archives) so they can be delivered as
first-class chat attachments (file/image keys) instead of dead local paths.
"""
from __future__ import annotations

import json
import os
import ntpath
import re
from pathlib import Path, PureWindowsPath
from typing import Dict, List, Optional, Tuple, Any

# Pattern matching absolute filesystem paths in assistant outputs (both files and directories)
PATH_PATTERN = re.compile(
    r'(?<![a-zA-Z0-9_\-\./])(/(?:home|data|tmp|root|opt|var|mnt|run)(?:/[\w\-\.]+)+)'
)
WINDOWS_QUOTED = re.compile(r'(?i)(?P<quote>[`"])(?P<path>[a-z]:\\users\\[^`"\r\n]+)(?P=quote)')
WINDOWS_UNQUOTED = re.compile(r'(?i)(?<![a-z0-9_])(?P<path>[a-z]:\\users\\[^\s\\`"<>|?*，。；：]+(?:\\[^\s\\`"<>|?*，。；：]+)*)')

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

    def _replace_path(raw_path: str) -> str:
        source = Path(raw_path)
        if base_dir is None:
            # Text-only rendering must be deterministic across retries, even
            # when the file is deleted or a symlink target later changes.
            parts = Path(os.path.normpath(raw_path)).parts
            if len(parts) >= 3 and parts[1] == 'home':
                return '.../' + '/'.join(parts[3:]) if len(parts) > 3 else '...'
            return '.../' + parts[-1] if len(parts) > 1 else '...'
        try:
            p = source.resolve()
        except (OSError, RuntimeError):
            # A broken or looping symlink must not block delivery of the receipt.
            return '.../' + source.name

        # Handle directory
        if p.exists() and p.is_dir():
            parts = p.parts
            if len(parts) >= 3 and parts[1] == "home":
                return ".../" + "/".join(parts[3:]) if len(parts) > 3 else "..."
            return ".../" + p.name

        # Check file existence
        exists = p.exists() and p.is_file()
        if not exists and not allow_nonexistent:
            # If path does not exist, sanitize out server-specific home and system paths
            parts = p.parts
            if len(parts) >= 3 and parts[1] == "home":
                return ".../" + "/".join(parts[3:]) if len(parts) > 3 else "..."
            return ".../" + "/".join(parts[2:]) if len(parts) > 2 else p.name

        # Text produced by a model is not authority to read arbitrary host files.
        # Only files directly in the caller-supplied attempt directory qualify;
        # DirectCompletionTransport performs the final no-symlink snapshot.
        allowed = (base_dir is not None and not source.is_symlink()
                   and not source.name.startswith('.')
                   and source.name not in {'manifest.json', 'report.json'}
                   and p.parent == Path(base_dir).resolve())
        if not allowed:
            parts = p.parts
            if len(parts) >= 3 and parts[1] == "home":
                return ".../" + "/".join(parts[3:]) if len(parts) > 3 else "..."
            return ".../" + p.name

        ext = '.tar.gz' if p.name.lower().endswith('.tar.gz') else p.suffix.lower()
        if ext in IMAGE_EXTS:
            if str(p) not in seen_paths:
                artifacts.append({"kind": "image", "path": str(p), "name": p.name})
                seen_paths.add(str(p))
            return f"📷 [图片附件候选: {p.name}]"
        elif ext in DOC_FILE_EXTS:
            if str(p) not in seen_paths:
                artifacts.append({"kind": "file", "path": str(p), "name": p.name})
                seen_paths.add(str(p))
            return f"📎 [文件附件候选: {p.name}]"
        else:
            # Code files or unknown extensions: sanitize server home
            parts = p.parts
            if len(parts) >= 3 and parts[1] == "home":
                return ".../" + "/".join(parts[3:]) if len(parts) > 3 else "..."
            return ".../" + p.name

    # Perform replacement
    def _replace_match(match: re.Match) -> str:
        raw_path = match.group(1)
        trimmed = raw_path.rstrip('.')
        return _replace_path(trimmed) + raw_path[len(trimmed):]

    sanitized = PATH_PATTERN.sub(_replace_match, text)
    def _redact_windows(raw: str) -> str:
        parts = PureWindowsPath(ntpath.normpath(raw)).parts
        return '.../' + '/'.join(parts[3:]) if len(parts) > 3 else '...'

    sanitized = WINDOWS_QUOTED.sub(
        lambda match: match.group('quote') + _redact_windows(match.group('path')) + match.group('quote'),
        sanitized)
    sanitized = WINDOWS_UNQUOTED.sub(lambda match: _redact_windows(match.group('path')), sanitized)
    return sanitized, artifacts


def unwrap_and_unescape_text(text: Any) -> str:
    """Single source of truth for unescaping \\uXXXX sequences and unwrapping JSON summary envelopes."""
    if text is None:
        return ""
    if not isinstance(text, str):
        if isinstance(text, dict):
            for _ in range(3):
                if isinstance(text.get("result"), dict):
                    text = text["result"]
                elif isinstance(text.get("checkpoint"), dict):
                    text = text["checkpoint"]
            for key in ("summary", "final_text", "report", "report_md", "checkpoint_summary", "message", "output", "reason", "error"):
                val = text.get(key)
                if isinstance(val, str) and val.strip():
                    return unwrap_and_unescape_text(val)
        try:
            text = json.dumps(text, ensure_ascii=False)
        except Exception:
            text = str(text)

    if "\\u" in text or "\\U" in text:
        try:
            text = re.sub(
                r'\\u([0-9a-fA-F]{4})',
                lambda m: chr(int(m.group(1), 16)),
                text,
            )
        except Exception:
            pass

    stripped = text.strip()
    json_start = stripped.find("{")
    if json_start != -1 and stripped.endswith("}"):
        candidate_json = stripped[json_start:]
        try:
            parsed = json.loads(candidate_json)
            if isinstance(parsed, dict):
                for _ in range(3):
                    if isinstance(parsed.get("result"), dict):
                        parsed = parsed["result"]
                    elif isinstance(parsed.get("checkpoint"), dict):
                        parsed = parsed["checkpoint"]
                for key in ("summary", "final_text", "report", "report_md", "checkpoint_summary", "output"):
                    val = parsed.get(key)
                    if isinstance(val, str) and val.strip():
                        prefix = stripped[:json_start].strip()
                        human_val = unwrap_and_unescape_text(val)
                        return f"{prefix}\n{human_val}".strip() if prefix else human_val
        except Exception:
            pass

    return stripped


def sanitize_card_text(text: Any) -> str:
    """Ensure user-facing text is cleanly decoded, unescaped, unwrapped from raw JSON, and path-sanitized."""
    unwrapped = unwrap_and_unescape_text(text)
    if not unwrapped:
        return ""
    sanitized, _ = sanitize_and_extract_artifacts(unwrapped)
    return sanitized
