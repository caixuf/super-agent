"""Shared pure reply renderer for card replies and final summaries.

This module has no transport, profile, environment mutation or sidecar dependency.
Patch builders embed these exact bytes; never fork a second Markdown parser.
"""
from __future__ import annotations

import os
import re
from typing import Any

# Cards render headings, tables, block quotes, horizontal rules and fenced code
# natively — verified against the live API on both card JSON 1.0 and 2.0 (all returned
# code=0). The old regex excluded them and cost every reply containing a code block or a
# `---` divider its card, which read as "the card UI is broken". Only the per-card table
# cap still forces a fallback, and that is counted exactly below.
_CARD_UNSUPPORTED_RE = re.compile(r"(?!x)x")  # matches nothing; kept for call-site compat
SHARED_CARD_RENDERER_VERSION = 1
# Lines inside a fence are code, not markdown: a `| a | b |` row in a shell snippet must
# not be lifted into a table element (it would be torn out of the block that renders it).
_CARD_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_CARD_TABLE_MAX = 5
_CARD_V2_ENV = "HERMES_FEISHU_CARD_V2"
_CARD24_ENV = "HERMES_FEISHU_CARD_24"


def _card_v2_enabled() -> bool:
    """Native tables need card JSON 2.0, which older clients render as '请升级客户端'.

    Opt-in per deployment: unset keeps the previous behaviour (complex markdown -> post).
    """
    return (os.environ.get(_CARD_V2_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def _split_pipe_cells(text: str) -> tuple[list[str], list[int]]:
    """Split on unescaped pipes and remember where those delimiters are.

    Markdown escaping is parity-based: one backslash escapes a pipe; two leave a literal
    backslash and the pipe is still a delimiter.  A regex split cannot express that, and
    treating every ``\\|`` as an escape silently changes both table shape and cell text.
    """
    cells: list[str] = []
    delimiters: list[int] = []
    current: list[str] = []
    escaped = False
    for index, char in enumerate(text):
        if char == "|" and not escaped:
            delimiters.append(index)
            cells.append("".join(current).strip())
            current = []
        else:
            if char == "|" and escaped:
                current.pop()  # remove only the Markdown pipe escape
            current.append(char)
        escaped = char == "\\" and not escaped
    cells.append("".join(current).strip())
    return cells, delimiters


def _md_table_cells(line: str) -> list[str]:
    """Split a table row into cells, dropping optional outer pipe delimiters."""
    text = line.strip()
    cells, delimiters = _split_pipe_cells(text)
    if delimiters and delimiters[0] == 0:
        cells.pop(0)
    # A literal escaped trailing pipe is data, not an optional outer delimiter.  The
    # delimiter list already tells us the parity, so do not infer it from ``rstrip``.
    if delimiters and delimiters[-1] == len(text) - 1:
        cells.pop()
    return cells


def _table_row_cells(line: str) -> list[str] | None:
    """Return cells only for a complete outer-pipe row; ``None`` means malformed."""
    text = line.strip()
    if not text.startswith("|"):
        return None
    _, delimiters = _split_pipe_cells(text)
    if len(delimiters) < 2 or delimiters[-1] != len(text) - 1:
        return None
    return _md_table_cells(line)


def _table_divider_cells(line: str) -> list[str] | None:
    """Return divider cells when *line* is a pipe delimiter row."""
    cells = _md_table_cells(line)
    # GFM accepts a divider without outer pipes, but every table row must still agree on
    # its cell count.  Keeping this permissive for the divider avoids rejecting the common
    # ``header\\n---|---`` form while the row checks below protect cell text.
    if not cells or not all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells):
        return None
    return cells


def _fenced_line_flags(lines: list[str]) -> list[bool]:
    """Respect fence type and length; nested shorter markers remain code."""
    flags: list[bool] = []
    marker = ""
    for line in lines:
        line = line.rstrip("\r\n")
        opening = _CARD_FENCE_OPEN_RE.match(line)
        if marker:
            flags.append(True)
            if opening and opening[1][0] == marker[0] and len(opening[1]) >= len(marker) and not opening[2].strip():
                marker = ""
        elif opening and not (opening[1][0] == "`" and "`" in opening[2]):
            marker = opening[1]
            flags.append(True)
        else:
            flags.append(False)
    return flags


def _card_table_candidates(lines: list[str]) -> list[tuple[int, int, dict[str, Any] | None]]:
    """Find table-looking blocks, including malformed ones that must stay as prose."""
    candidates: list[tuple[int, int, dict[str, Any] | None]] = []
    fenced = _fenced_line_flags(lines)
    index = 0
    while index < len(lines) - 1:
        line = lines[index]
        if fenced[index] or _table_row_cells(line) is None:
            index += 1
            continue
        divider = _table_divider_cells(lines[index + 1])
        if divider is None:
            index += 1
            continue
        stop = index + 2
        # A pipe-looking line directly after the table is part of its candidate block even
        # when malformed.  Leaving it to a later prose slice could convert the valid prefix
        # while silently changing the meaning of the malformed row.
        while stop < len(lines) and not fenced[stop] and lines[stop].lstrip().startswith("|"):
            stop += 1
        header = _table_row_cells(line)
        assert header is not None
        data_rows = [_table_row_cells(text) for text in lines[index + 2:stop]]
        valid = (len(header) == len(divider)
                 and all(row is not None and len(row) <= len(header) for row in data_rows))
        if valid:
            rows = [{"c%d" % n: (row[n] if n < len(row) else "")
                     for n in range(len(header))}
                    for row in data_rows]
            candidates.append((index, stop, {
                "tag": "table", "page_size": 10, "row_height": "low",
                "freeze_first_column": len(header) > 2,
                "header_style": {"text_size": "normal", "background_style": "grey", "bold": True},
                "columns": [{"name": "c%d" % n, "display_name": header[n] or " ",
                             "data_type": "lark_md", "width": "auto", "vertical_align": "top"}
                            for n in range(len(header))],
                "rows": rows,
            }))
        else:
            candidates.append((index, stop, None))
        index = stop
    return candidates


def _card_tables(lines: list[str]) -> list[tuple[int, int, dict[str, Any]]]:
    """Locate renderable GFM pipe tables: ``[(start, stop, element)]``.

    Fenced regions are skipped: a pipe-shaped row inside a shell/python snippet is code
    that the markdown block must render verbatim, not a table to lift out of it.
    """
    found: list[tuple[int, int, dict[str, Any]]] = []
    for start, stop, table in _card_table_candidates(lines):
        if table is not None and len(found) < _CARD_TABLE_MAX:
            found.append((start, stop, table))
    return found


def build_card_elements(content: str) -> list[dict[str, Any]]:
    """One markdown reply -> card elements: markdown blocks with native tables in place."""
    if not content:
        return [{"tag": "markdown", "content": " "}]
    lines = content.splitlines()
    raw_lines = content.splitlines(keepends=True)
    elements: list[dict[str, Any]] = []
    cursor = 0
    for start, stop, table in _card_tables(lines):
        prose = "".join(raw_lines[cursor:start])
        if prose.strip():
            elements.append({"tag": "markdown", "content": prose})
        elements.append(table)
        cursor = stop
    tail = "".join(raw_lines[cursor:])
    if tail.strip() or not elements:
        elements.append({"tag": "markdown", "content": tail or " "})
    return elements


_CARD24_INLINE_MD_RE = re.compile(r"\*\*(.+?)\*\*|`([^`]+)`|\[([^\]]+)\]\(([^)]+)\)")


def _card24_text(content: str) -> str:
    """Markdown -> the plain-text subset card_components v2 accepts (no markdown tag)."""
    text = _CARD24_INLINE_MD_RE.sub(
        lambda m: m.group(1) or m.group(2) or ("%s (%s)" % (m.group(3), m.group(4))), content or "")
    text = text.replace("**", "").replace("`", "")
    return re.sub(r"(?m)^#{1,6}\s+", "", text)


def card_components_v2() -> bool:
    """Card 2.4 ('card_components' v2) rejects the 1.0 ``markdown``/``note`` tags.

    Opt-in per deployment; the 2.0 path (markdown + table components) stays the default.
    """
    return (os.environ.get(_CARD24_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def build_card24_elements(content: str) -> list[dict[str, Any]]:
    """Same split as the 2.0 builder: plain_text blocks, tables with plain-text cells."""
    elements = build_card_elements(content)
    for element in elements:
        if element.get("tag") != "table":
            continue
        for column in element["columns"]:
            column["data_type"] = "text"
        for row in element["rows"]:
            for column in element["columns"]:
                row[column["name"]] = _card24_text(str(row.get(column["name"], "")))
    return [element if element.get("tag") == "table"
            else {"tag": "div",
                  "text": {"tag": "plain_text", "content": _card24_text(element.get("content", "")) or " "}}
            for element in elements]


def card_renders_markdown(text: str) -> bool:
    """True when every construct in *text* has a native card representation."""
    text = text or ""
    if _CARD_UNSUPPORTED_RE.search(text):
        return False
    lines = text.splitlines()
    candidates = _card_table_candidates(lines)
    # A malformed table or a sixth table would remain Markdown prose.  Keep those replies on
    # the post path instead of rendering a partially converted card.
    return len(candidates) <= _CARD_TABLE_MAX and all(table is not None
                                                      for _start, _stop, table in candidates)
