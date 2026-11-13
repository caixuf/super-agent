"""Structured backend-to-frontdesk completion report protocol (v1).

Provides a single canonical schema, parser, validator, and Markdown renderer
for backend colleague reports containing:
- conclusion: concise verdict / takeaway
- highlights: key findings or metrics
- files: deliverable file attachments
- images: charts / screenshots to render or attach
- links: clickable external/internal URLs (docs, issue trackers, reports)
- next_actions: recommended follow-ups or human decisions
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

SCHEMA_VERSION = "1.1"
MAX_HIGHLIGHTS = 12
MAX_FILES = 8
MAX_IMAGES = 8
MAX_LINKS = 12
MAX_NEXT_ACTIONS = 8
MAX_PART_IDS = 64

_VALID_PART_STATES = frozenset({"pending", "ready", "failed", "skipped"})

_REPORT_FENCE_RE = re.compile(
    r"```json:report\s*\n(.*?)\n```|<report_json>\s*(.*?)\s*</report_json>",
    re.DOTALL | re.IGNORECASE,
)

_INTERNAL_FILES = frozenset({
    "worker_result.json",
    "worker_stdout.log",
    "worker_stderr.log",
    "attempt_manifest.json",
    "attempt.json",
})


def _clean_str(value: Any, limit: int = 2000) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if len(text) > limit:
        return text[: limit - 3].rstrip() + "..."
    return text


def _normalize_str_list(raw: Any, *, limit: int, item_len: int = 500) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        s = _clean_str(item, item_len)
        if s:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def _normalize_files(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            path = _clean_str(item, 4096)
            name = os.path.basename(path) if path else ""
            desc = ""
        elif isinstance(item, dict):
            path = _clean_str(item.get("path"), 4096)
            name = _clean_str(item.get("name"), 256) or (os.path.basename(path) if path else "")
            desc = _clean_str(item.get("description") or item.get("purpose"), 300)
        else:
            continue
        if not path or os.path.basename(path) in _INTERNAL_FILES or path in seen:
            continue
        seen.add(path)
        entry: dict[str, str] = {"path": path, "name": name}
        if desc:
            entry["description"] = desc
        out.append(entry)
        if len(out) >= MAX_FILES:
            break
    return out


def _normalize_images(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            path = _clean_str(item, 4096)
            caption = ""
        elif isinstance(item, dict):
            path = _clean_str(item.get("path") or item.get("url"), 4096)
            caption = _clean_str(item.get("caption") or item.get("title"), 300)
        else:
            continue
        if not path or os.path.basename(path) in _INTERNAL_FILES or path in seen:
            continue
        seen.add(path)
        entry: dict[str, str] = {"path": path}
        if caption:
            entry["caption"] = caption
        out.append(entry)
        if len(out) >= MAX_IMAGES:
            break
    return out


def _normalize_links(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            url = _clean_str(item, 2048)
            title = url
        elif isinstance(item, dict):
            url = _clean_str(item.get("url") or item.get("href"), 2048)
            title = _clean_str(item.get("title") or item.get("label"), 200) or url
        else:
            continue
        if not url or not (url.startswith("http://") or url.startswith("https://")) or url in seen:
            continue
        seen.add(url)
        out.append({"title": title, "url": url})
        if len(out) >= MAX_LINKS:
            break
    return out


def _normalize_parts_status(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        ks = _clean_str(k, 40)
        vs = _clean_str(v, 20).lower()
        if ks and vs in _VALID_PART_STATES:
            out[ks] = vs
    return out


def normalize_structured_report(raw: Any) -> dict[str, Any] | None:
    """Validate and normalize a structured report (full or incremental delta); return None if empty."""
    if not isinstance(raw, dict):
        return None
    conclusion = _clean_str(raw.get("conclusion") or raw.get("verdict"), 2000)
    highlights = _normalize_str_list(raw.get("highlights") or raw.get("findings"), limit=MAX_HIGHLIGHTS)
    files = _normalize_files(raw.get("files"))
    images = _normalize_images(raw.get("images"))
    links = _normalize_links(raw.get("links"))
    next_actions = _normalize_str_list(raw.get("next_actions"), limit=MAX_NEXT_ACTIONS)
    parts_status = _normalize_parts_status(raw.get("parts_status"))
    has_final = "final" in raw
    final = bool(raw.get("final", True))
    part_id = _clean_str(raw.get("part_id"), 80)
    try:
        seq = int(raw.get("seq", 0) or 0)
    except (TypeError, ValueError):
        seq = 0

    if not any((conclusion, highlights, files, images, links, next_actions, parts_status, has_final)):
        return None
    report: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "final": final}
    if seq > 0:
        report["seq"] = seq
    if part_id:
        report["part_id"] = part_id
    if parts_status:
        report["parts_status"] = parts_status
    if conclusion:
        report["conclusion"] = conclusion
    if highlights:
        report["highlights"] = highlights
    if files:
        report["files"] = files
    if images:
        report["images"] = images
    if links:
        report["links"] = links
    if next_actions:
        report["next_actions"] = next_actions
    return report


def _merge_dict_items(
    existing: list[dict[str, str]],
    incoming: list[dict[str, str]],
    *,
    key_field: str,
    limit: int,
) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = [dict(x) for x in existing]
    index = {item[key_field]: idx for idx, item in enumerate(merged) if key_field in item}
    for item in incoming:
        k = item.get(key_field)
        if not k:
            continue
        if k in index:
            merged[index[k]].update({ik: iv for ik, iv in item.items() if iv})
        elif len(merged) < limit:
            index[k] = len(merged)
            merged.append(dict(item))
    return merged


def merge_structured_report(existing: dict[str, Any] | None, delta: dict[str, Any] | None) -> dict[str, Any] | None:
    """Idempotently merge an async/incremental report delta into an existing report."""
    norm_delta = normalize_structured_report(delta) if delta is not None else None
    norm_base = normalize_structured_report(existing) if existing is not None else None
    if norm_delta is None:
        return norm_base
    if norm_base is None:
        base: dict[str, Any] = dict(norm_delta)
        if norm_delta.get("part_id"):
            base["_applied_parts"] = [norm_delta["part_id"]]
        return base

    applied_parts = list(existing.get("_applied_parts") or []) if isinstance(existing, dict) else []
    part_id = norm_delta.get("part_id", "")
    if part_id and part_id in applied_parts:
        out_same = dict(norm_base)
        if applied_parts:
            out_same["_applied_parts"] = applied_parts[-MAX_PART_IDS:]
        return out_same

    out: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "final": bool(norm_base.get("final", False) or norm_delta.get("final", False)),
    }
    base_seq = int(norm_base.get("seq", 0) or 0)
    delta_seq = int(norm_delta.get("seq", 0) or 0)
    if max(base_seq, delta_seq) > 0:
        out["seq"] = max(base_seq, delta_seq)

    # Conclusion updates if newer or base was empty.
    if norm_delta.get("conclusion") and (delta_seq == 0 or delta_seq >= base_seq or not norm_base.get("conclusion")):
        out["conclusion"] = norm_delta["conclusion"]
    elif norm_base.get("conclusion"):
        out["conclusion"] = norm_base["conclusion"]

    for list_key, limit in (("highlights", MAX_HIGHLIGHTS), ("next_actions", MAX_NEXT_ACTIONS)):
        combined = list(norm_base.get(list_key) or [])
        seen = set(combined)
        for item in norm_delta.get(list_key) or []:
            if item not in seen and len(combined) < limit:
                seen.add(item)
                combined.append(item)
        if combined:
            out[list_key] = combined

    files = _merge_dict_items(norm_base.get("files") or [], norm_delta.get("files") or [], key_field="path", limit=MAX_FILES)
    if files:
        out["files"] = files
    images = _merge_dict_items(norm_base.get("images") or [], norm_delta.get("images") or [], key_field="path", limit=MAX_IMAGES)
    if images:
        out["images"] = images
    links = _merge_dict_items(norm_base.get("links") or [], norm_delta.get("links") or [], key_field="url", limit=MAX_LINKS)
    if links:
        out["links"] = links

    parts_status = dict(norm_base.get("parts_status") or {})
    parts_status.update(norm_delta.get("parts_status") or {})
    if parts_status:
        out["parts_status"] = parts_status

    if part_id:
        applied_parts.append(part_id)
    if applied_parts:
        out["_applied_parts"] = applied_parts[-MAX_PART_IDS:]
    return out


def extract_report_from_text(text: str) -> tuple[dict[str, Any] | None, str]:
    """Extract an embedded ``json:report`` or ``<report_json>`` block from text."""
    if not isinstance(text, str) or not text.strip():
        return None, ""
    match = _REPORT_FENCE_RE.search(text)
    if not match:
        return None, text
    raw_json = (match.group(1) or match.group(2) or "").strip()
    try:
        parsed = json.loads(raw_json)
    except ValueError:
        return None, text
    normalized = normalize_structured_report(parsed)
    if normalized is None:
        return None, text
    remainder = (text[: match.start()] + text[match.end() :]).strip()
    return normalized, remainder


def parse_structured_report(result: Any) -> tuple[dict[str, Any] | None, str]:
    """Extract normalized structured_report from a worker result dict or text."""
    if isinstance(result, dict):
        embedded = result.get("structured_report")
        if isinstance(embedded, dict):
            norm = normalize_structured_report(embedded)
            if norm is not None:
                return norm, ""
        # Also allow top-level structured keys on the result dict itself.
        if any(k in result for k in ("conclusion", "links", "images", "files", "highlights")):
            norm = normalize_structured_report(result)
            if norm is not None:
                return norm, ""
        for key in ("summary", "final_text", "report", "report_md", "output", "message"):
            val = result.get(key)
            if isinstance(val, str) and val.strip():
                norm, rem = extract_report_from_text(val)
                if norm is not None:
                    return norm, rem
        return None, ""
    if isinstance(result, str):
        return extract_report_from_text(result)
    return None, ""


def extract_attachment_paths(report: dict[str, Any] | None) -> list[str]:
    """Return local file/image paths from a structured report for transport staging."""
    if not isinstance(report, dict):
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for item in report.get("files") or []:
        p = item.get("path") if isinstance(item, dict) else None
        if isinstance(p, str) and p and p not in seen:
            seen.add(p)
            paths.append(p)
    for item in report.get("images") or []:
        p = item.get("path") if isinstance(item, dict) else None
        if isinstance(p, str) and p and not p.startswith(("http://", "https://")) and p not in seen:
            seen.add(p)
            paths.append(p)
    return paths[:MAX_FILES]


def render_report_markdown(report: dict[str, Any]) -> str:
    """Render a normalized structured report into clean conclusion-first Markdown."""
    sections: list[str] = []
    if report.get("conclusion"):
        sections.append(f"**结论**：{report['conclusion']}")
    if report.get("highlights"):
        bullets = "\n".join(f"- {item}" for item in report["highlights"])
        sections.append(f"**关键结果**：\n{bullets}")
    if report.get("links"):
        items = "\n".join(f"- [{lnk['title']}]({lnk['url']})" for lnk in report["links"])
        sections.append(f"**相关链接**：\n{items}")
    if report.get("images"):
        imgs = "\n".join(
            f"- {img.get('caption') or os.path.basename(img['path'])}: `{img['path']}`"
            for img in report["images"]
        )
        sections.append(f"**图表/截图**：\n{imgs}")
    if report.get("files"):
        flist = "\n".join(
            f"- `{f['name']}`" + (f" — {f['description']}" if f.get("description") else "")
            for f in report["files"]
        )
        sections.append(f"**交付文件**：\n{flist}")
    if report.get("next_actions"):
        acts = "\n".join(f"- {act}" for act in report["next_actions"])
        sections.append(f"**下一步建议**：\n{acts}")
    parts_status = report.get("parts_status") if isinstance(report.get("parts_status"), dict) else {}
    pending = [k for k, v in parts_status.items() if v == "pending"]
    if not report.get("final", True) and pending:
        sections.append(f"*异步生成中：{', '.join(pending)}*")
    return "\n\n".join(sections)

