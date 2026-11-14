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
            return f"[图片附件候选: {p.name}]"
        elif ext in DOC_FILE_EXTS:
            if str(p) not in seen_paths:
                artifacts.append({"kind": "file", "path": str(p), "name": p.name})
                seen_paths.add(str(p))
            return f"[文件附件候选: {p.name}]"
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


_LEAKED_MODEL_TOKEN_RE = re.compile(r"<\|[A-Za-z0-9_.:/-]{1,64}\|>")
_LEADING_MODEL_TOKEN_RE = re.compile(r"^([ \t]*)(?:<\|[A-Za-z0-9_.:/-]{1,64}\|>[ \t]*)+")
_FENCE_OPEN_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_DIVIDER_CELL_RE = re.compile(r"^:?-{1,}:?$")


def _fence_close_matches(stripped: str, active_fence: str) -> bool:
    return bool(active_fence) and len(stripped) >= len(active_fence) and set(stripped) == {active_fence[0]}


def _split_pipe_cells(raw_line: str) -> Optional[List[str]]:
    s = raw_line.strip()
    if "|" not in s:
        return None
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [cell.strip() for cell in s.split("|")]


def _parse_pipe_divider(raw_line: str) -> Optional[int]:
    s = raw_line.strip()
    if "|" not in s:
        return None
    cells = _split_pipe_cells(s)
    if not cells:
        return None
    if not all(_DIVIDER_CELL_RE.fullmatch(c) for c in cells):
        return None
    if not any("--" in c for c in cells):
        return None
    if len(cells) < 2 and not (s.startswith("|") and s.endswith("|")):
        return None
    return len(cells)


def _normalize_pipe_row(raw_line: str) -> str:
    leading = raw_line[: len(raw_line) - len(raw_line.lstrip(" \t"))]
    s = raw_line.strip()
    if not s.startswith("|"):
        s = f"| {s}"
    if not s.endswith("|"):
        s = f"{s} |"
    return f"{leading}{s}"


def _normalize_divider_row(raw_line: str) -> str:
    leading = raw_line[: len(raw_line) - len(raw_line.lstrip(" \t"))]
    s = raw_line.strip()
    if not s.startswith("|"):
        s = f"|{s}"
    if not s.endswith("|"):
        s = f"{s}|"
    return f"{leading}{s}"


def _default_table_header(ncols: int) -> str:
    if ncols <= 1:
        return "| 项目 |"
    if ncols == 2:
        return "| 项目 | 结果 |"
    extra = " | ".join(f"列{i}" for i in range(3, ncols + 1))
    return f"| 项目 | 结果 | {extra} |"


def strip_leaked_model_tokens(text: str) -> str:
    """Strip leaked special model tokens (e.g. <|channel|>) outside fenced code blocks.

    When a leading leaked token replaces the opening pipe of a Markdown table header/row
    (such as `<|channel|> 项 | 结果 |`), restore the leading `|` instead of breaking the table.
    """
    if not isinstance(text, str) or "<|" not in text:
        return text

    lines = text.splitlines(keepends=True)
    out: List[str] = []
    active_fence = ""

    for idx, line_with_end in enumerate(lines):
        if line_with_end.endswith("\r\n"):
            line, eol = line_with_end[:-2], "\r\n"
        elif line_with_end.endswith("\n"):
            line, eol = line_with_end[:-1], "\n"
        else:
            line, eol = line_with_end, ""

        stripped = line.strip()
        if active_fence:
            if _fence_close_matches(stripped, active_fence):
                active_fence = ""
            out.append(line + eol)
            continue

        fence_m = _FENCE_OPEN_RE.match(line)
        if fence_m:
            active_fence = fence_m.group(1)
            out.append(line + eol)
            continue

        if "<|" not in line:
            out.append(line + eol)
            continue

        lead_m = _LEADING_MODEL_TOKEN_RE.match(line)
        if lead_m:
            indent = lead_m.group(1)
            rest = _LEAKED_MODEL_TOKEN_RE.sub("", line[lead_m.end():]).strip()
            if not rest:
                continue
            next_is_divider = False
            if idx + 1 < len(lines):
                next_raw = lines[idx + 1].rstrip("\r\n")
                next_clean = _LEADING_MODEL_TOKEN_RE.sub("|", _LEAKED_MODEL_TOKEN_RE.sub("", next_raw))
                next_is_divider = _parse_pipe_divider(next_clean) is not None or _parse_pipe_divider(next_raw) is not None

            if "|" in rest and (rest.endswith("|") or next_is_divider or _parse_pipe_divider("|" + rest) is not None):
                if rest.startswith("|"):
                    repaired = f"{indent}{rest}"
                elif _parse_pipe_divider("|" + rest) is not None:
                    repaired = f"{indent}|{rest.lstrip()}"
                else:
                    repaired = f"{indent}| {rest}"
                out.append(repaired + eol)
                continue

            cleaned_rest = re.sub(r"[ \t]{2,}", " ", rest).strip()
            out.append(f"{indent}{cleaned_rest}" + eol)
            continue

        indent = line[: len(line) - len(line.lstrip(" \t"))]
        body = _LEAKED_MODEL_TOKEN_RE.sub("", line[len(indent):])
        body = re.sub(r"[ \t]{2,}", " ", body).strip()
        out.append((f"{indent}{body}" if body else "") + eol)

    return "".join(out)


def repair_headerless_pipe_tables(text: str) -> str:
    """Normalize half-piped tables and prepend a default header to orphan `|---|---|` tables."""
    if not isinstance(text, str) or "|" not in text or "--" not in text:
        return text

    raw_lines = text.splitlines(keepends=True)
    lines: List[Tuple[str, str]] = []
    for item in raw_lines:
        if item.endswith("\r\n"):
            lines.append((item[:-2], "\r\n"))
        elif item.endswith("\n"):
            lines.append((item[:-1], "\n"))
        else:
            lines.append((item, ""))

    out: List[Tuple[str, str]] = []
    active_fence = ""
    i = 0
    n = len(lines)

    while i < n:
        line, eol = lines[i]
        stripped = line.strip()

        if active_fence:
            if _fence_close_matches(stripped, active_fence):
                active_fence = ""
            out.append((line, eol))
            i += 1
            continue

        fence_m = _FENCE_OPEN_RE.match(line)
        if fence_m:
            active_fence = fence_m.group(1)
            out.append((line, eol))
            i += 1
            continue

        ncols = _parse_pipe_divider(line)
        if ncols is None or i + 1 >= n:
            out.append((line, eol))
            i += 1
            continue

        next_line, _ = lines[i + 1]
        next_stripped = next_line.strip()
        if not next_stripped or "|" not in next_stripped or _parse_pipe_divider(next_line) is not None:
            out.append((line, eol))
            i += 1
            continue

        has_header = False
        if out:
            prev_line, prev_eol = out[-1]
            prev_cells = _split_pipe_cells(prev_line)
            if (
                prev_cells is not None
                and _parse_pipe_divider(prev_line) is None
                and not _FENCE_OPEN_RE.match(prev_line)
                and len(prev_cells) == ncols
            ):
                out[-1] = (_normalize_pipe_row(prev_line), prev_eol)
                has_header = True

        if not has_header:
            divider_eol = eol or "\n"
            if out and out[-1][0].strip():
                out.append(("", divider_eol))
            out.append((_default_table_header(ncols), divider_eol))

        out.append((_normalize_divider_row(line), eol))
        i += 1

        while i < n:
            row_line, row_eol = lines[i]
            row_stripped = row_line.strip()
            if not row_stripped or "|" not in row_stripped or _FENCE_OPEN_RE.match(row_line):
                break
            if _parse_pipe_divider(row_line) is not None:
                break
            out.append((_normalize_pipe_row(row_line), row_eol))
            i += 1

    return "".join(line + eol for line, eol in out)


def _clean_human_markdown(text: str) -> str:
    return repair_headerless_pipe_tables(strip_leaked_model_tokens(text))


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
                        combined = f"{prefix}\n{human_val}".strip() if prefix else human_val
                        return _clean_human_markdown(combined)
        except Exception:
            pass

    return _clean_human_markdown(stripped)


def sanitize_card_text(text: Any) -> str:
    """Ensure user-facing text is cleanly decoded, unescaped, unwrapped from raw JSON, and path-sanitized."""
    unwrapped = unwrap_and_unescape_text(text)
    if not unwrapped:
        return ""
    sanitized, _ = sanitize_and_extract_artifacts(unwrapped)
    return sanitized
