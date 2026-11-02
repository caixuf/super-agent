"""Final-only HFC projection backed by the shared Markdown card renderer.

This module is bundled as ``hermes_feishu_card/shared_final_render.py``.  It is
deliberately transport-free: it only rewrites an already rendered, safely
admitted card and returns a new :class:`CardRenderResult`.
"""
from __future__ import annotations

import copy
from dataclasses import replace
import re
from typing import Any

from .card_limits import inspect_card_limits
from .render import CardRenderResult, MAIN_CONTENT_CHUNK_CHARS
from .shared_card_markdown import build_card_elements, card_renders_markdown
from .text import normalize_stream_text, split_markdown_blocks


def project_final_result(
    result: CardRenderResult,
    session: Any,
    *,
    title: str,
    enabled: bool = False,
) -> CardRenderResult:
    """Project a completed chat answer through the shared Markdown renderer.

    The projection is intentionally narrow.  It runs only for an opted-in,
    completed chat card whose canonical answer is non-empty, and it replaces
    only the existing ``main_content`` elements.  The final header is also
    deliberately normalized to a plain green title and the schema-2.0 width to
    fill; every other element (footer, execution timeline and controls) and
    every other config field is retained.  Any uncertainty returns the original
    result unchanged.
    """
    if enabled is not True or not _eligible_session(session):
        return result
    if getattr(result, "disposition", "") != "card":
        return result
    card = getattr(result, "card", None)
    if not isinstance(card, dict) or card.get("schema") != "2.0":
        return result
    if not isinstance(title, str) or not title.strip():
        return result

    try:
        answer = normalize_stream_text(session.answer_text)
    except Exception:
        return result
    if not isinstance(answer, str) or not answer.strip():
        return result
    if not card_renders_markdown(answer):
        return result

    try:
        projected = _shared_main_content(answer)
    except Exception:
        return result
    if projected is None:
        return result

    new_card = copy.deepcopy(card)
    config = new_card.get("config")
    body = new_card.get("body")
    elements = body.get("elements") if isinstance(body, dict) else None
    if not isinstance(config, dict) or not isinstance(elements, list):
        return result
    if any(not isinstance(element, dict) for element in elements):
        return result

    main_indexes = [
        index for index, element in enumerate(elements)
        if _is_main_content(element)
    ]
    if not main_indexes:
        return result
    if _has_generated_mention((elements[index] for index in main_indexes), answer):
        # HFC may add a verified requester/completion mention to the rendered
        # body.  The shared projection would replace it with the canonical
        # answer, silently dropping that notification.  Keep HFC's rendition.
        return result

    main_index_set = set(main_indexes)
    first_main = main_indexes[0]
    replaced = [
        element for index, element in enumerate(elements)
        if index not in main_index_set
    ]
    replaced[first_main:first_main] = projected
    body["elements"] = replaced

    # Final cards use the schema-2.0 wide mode.  No legacy schema conversion is
    # performed here, and HFC's existing config switches (update_multi, styles,
    # streaming_mode, summary) remain untouched.
    config["width_mode"] = "fill"
    new_card["header"] = {
        "template": "green",
        "title": {"tag": "plain_text", "content": title.strip()},
    }

    try:
        inspection = inspect_card_limits(new_card)
    except Exception:
        return result
    if not inspect_safe(inspection):
        return result

    try:
        return replace(result, card=new_card, inspection=inspection, primary_text=answer)
    except Exception:
        return result


def _eligible_session(session: Any) -> bool:
    """Return whether this is a plain completed chat turn with a real answer."""
    if getattr(session, "status", "") != "completed":
        return False
    if getattr(session, "delivery_kind", "") != "chat":
        return False
    if getattr(session, "execution_scope", "") == "cron":
        return False
    if getattr(session, "active_interaction", None) is not None:
        return False
    if getattr(session, "notice_kind", "") or getattr(session, "notice_title", ""):
        return False
    if getattr(session, "terminal_disposition", "") == "native":
        return False
    if getattr(session, "terminal_handoff_record", None) is not None:
        return False
    answer = getattr(session, "answer_text", None)
    return isinstance(answer, str) and bool(answer.strip())


def _shared_main_content(answer: str) -> list[dict[str, Any]] | None:
    """Build numbered main-content elements without reparsing Markdown."""
    projected: list[dict[str, Any]] = []
    try:
        shared = build_card_elements(answer)
    except Exception:
        return None
    if not isinstance(shared, list) or not shared:
        return None

    for source in shared:
        if not isinstance(source, dict):
            return None
        tag = source.get("tag")
        if tag == "table":
            element = copy.deepcopy(source)
            element["element_id"] = _main_content_id(len(projected))
            projected.append(element)
            continue
        if tag != "markdown" or not isinstance(source.get("content"), str):
            return None
        try:
            chunks = split_markdown_blocks(
                source["content"], MAIN_CONTENT_CHUNK_CHARS,
            )
        except Exception:
            return None
        if not isinstance(chunks, list) or not chunks:
            return None
        for chunk in chunks:
            if not isinstance(chunk, str):
                return None
            projected.append({
                "tag": "markdown",
                "element_id": _main_content_id(len(projected)),
                "content": chunk,
                "text_size": "normal",
            })
    return projected or None


def _main_content_id(index: int) -> str:
    return "main_content" if index == 0 else f"main_content_{index}"


def _is_main_content(element: dict[str, Any]) -> bool:
    element_id = element.get("element_id")
    return isinstance(element_id, str) and (
        element_id == "main_content" or element_id.startswith("main_content_")
    )


_AT_MARKUP_RE = re.compile(r"<at(?=\s|>)", re.IGNORECASE)


def _has_generated_mention(elements: Any, answer: str) -> bool:
    """Detect body-only mentions HFC generated outside the canonical answer."""
    if _AT_MARKUP_RE.search(answer):
        return True
    for element in elements:
        content = element.get("content")
        if isinstance(content, str) and _AT_MARKUP_RE.search(content):
            return True
    return False


def inspect_safe(inspection: Any) -> bool:
    """Accept only an explicitly safe whole-card inspection."""
    return getattr(inspection, "safe", False) is True
