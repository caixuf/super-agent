"""agentcore.cardkit2_renderer — Native CardKit 2.0 streaming progress & final delivery renderer.

Ported from proven production patterns (single-inflight async pipeline + CardKit 2.0
semantic background palettes + native collapsible stage panels). Completely decoupled
from external sidecars and business logic.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import unicodedata
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from agentcore.card_markdown import _fenced_line_flags, build_card_elements

logger = logging.getLogger(__name__)

# Zero-emoji engineering indicators conforming to Super Agent discipline:
# Strict ASCII & clean typographic symbols; no playful cartoons or emoji bloat.
INDICATOR_ACTIVE = "[BUSY]"
INDICATOR_DONE = "[OK]"
INDICATOR_ERROR = "[FAIL]"
INDICATOR_TIMER = "耗时:"
INDICATOR_STEP = "步数:"
INDICATOR_WARN = "[TIMEOUT-WARN]"

# Horizontal rule delimiter (clean typographic hairline)
DIVIDER = "────────────────"

# Low-frequency title color breathing cycle (calm terminal hues)
TITLE_COLORS = ("blue", "turquoise", "wathet", "blue-300")


def color_for_tick(tick: int) -> str:
    """Cycle title colors across render frames to produce a smooth breathing effect."""
    return TITLE_COLORS[tick % len(TITLE_COLORS)]


def extract_stages_and_clean_text(text: str) -> Tuple[str, str]:
    """Extract public ``##STAGE:`` lines while withholding private reasoning.

    Returns ``(stage_text, clean_text)``. A truncated or nested reasoning block
    is withheld too; collapsing it into a visible panel is not redaction.
    """
    raw = text or ""
    public: List[str] = []
    depth = 0
    cursor = 0
    for marker in re.finditer(r"<\s*(/?)\s*think\b[^>]*>", raw, flags=re.IGNORECASE):
        if not depth:
            public.append(raw[cursor:marker.start()])
        if marker.group(1):
            depth = max(0, depth - 1)
        else:
            depth += 1
        cursor = marker.end()
    if not depth:
        public.append(raw[cursor:])
    # Streaming snapshots can end in an incomplete opening marker.
    safe_text = re.sub(r"<\s*(?:t|th|thi|thin|think)\s*$", "",
                       "".join(public), flags=re.IGNORECASE)
    stages: List[str] = []
    lines: List[str] = []
    for line in safe_text.splitlines():
        if line.startswith("##STAGE:"):
            stages.append(line.replace("##STAGE:", "").strip())
        else:
            lines.append(line)
    clean = "\n".join(lines).strip()

    return "\n\n".join(s for s in stages if s).strip(), clean


NO_TRAILING_SPACE_CHARS = set("：，。；！？、）》】」』")
NO_LEADING_SPACE_CHARS = set("（《【「『")
_CARD_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _is_punctuation(ch: str) -> bool:
    return unicodedata.category(ch).startswith("P")


def _protect_code(text: str) -> Tuple[str, List[str]]:
    if not text:
        return text, []
    code_store: List[str] = []
    lines = text.split("\n")
    out_lines: List[str] = []
    in_fence = False
    fence_marker = ""
    fence_buf: List[str] = []

    for line in lines:
        stripped = line.rstrip("\r\n")
        m = _CARD_FENCE_RE.match(stripped)
        if in_fence:
            fence_buf.append(line)
            if m and m.group(1)[0] == fence_marker[0] and len(m.group(1)) >= len(fence_marker) and not m.group(2).strip():
                idx = len(code_store)
                code_store.append("\n".join(fence_buf))
                out_lines.append(f"\x00__CODE_{idx}__\x00")
                in_fence = False
                fence_marker = ""
                fence_buf = []
        elif m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            in_fence = True
            fence_marker = m.group(1)
            fence_buf = [line]
        else:
            out_lines.append(line)

    if in_fence:
        idx = len(code_store)
        code_store.append("\n".join(fence_buf))
        out_lines.append(f"\x00__CODE_{idx}__\x00")

    intermediate = "\n".join(out_lines)

    def _mask_inline_code(match: re.Match) -> str:
        idx = len(code_store)
        code_store.append(match.group(0))
        return f"\x00__CODE_{idx}__\x00"

    intermediate = re.sub(r"(`+)(.+?)\1", _mask_inline_code, intermediate, flags=re.DOTALL)
    return intermediate, code_store


def _restore_code(text: str, code_store: List[str]) -> str:
    for idx, code in enumerate(code_store):
        text = text.replace(f"\x00__CODE_{idx}__\x00", code)
    return text


def seal_unclosed_markdown_fences(text: str) -> str:
    """Seal any trailing unclosed Markdown code fences.

    Scans lines for code fence delimiters (``` or ~~~ with 0-3 leading spaces).
    If an unclosed code fence remains at EOF, appends a newline and matching closing fence.
    """
    if not text:
        return text or ""
    in_fence = False
    fence_marker = ""
    for line in text.splitlines():
        stripped = line.rstrip("\r\n")
        m = _CARD_FENCE_RE.match(stripped)
        if in_fence:
            if m and m.group(1)[0] == fence_marker[0] and len(m.group(1)) >= len(fence_marker) and not m.group(2).strip():
                in_fence = False
                fence_marker = ""
        elif m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            in_fence = True
            fence_marker = m.group(1)

    if in_fence and fence_marker:
        return (text if text.endswith("\n") else text + "\n") + fence_marker
    return text


def _is_standalone_divider(line: str) -> bool:
    stripped = line.strip()
    return stripped == DIVIDER or bool(re.match(r"^ {0,3}-{3,}\s*$", line.rstrip("\r\n")))


def _split_markdown_by_dividers(content: str) -> List[Dict[str, Any]]:
    """Split markdown content by unfenced standalone divider lines into markdown and hr tags."""
    lines = content.splitlines()
    fenced = _fenced_line_flags(lines)
    elements: List[Dict[str, Any]] = []
    chunk: List[str] = []
    for line, is_fenced in zip(lines, fenced):
        if not is_fenced and _is_standalone_divider(line):
            if chunk:
                text = "\n".join(chunk).strip()
                if text:
                    elements.append({"tag": "markdown", "content": text})
                chunk = []
            elements.append({"tag": "hr"})
        else:
            chunk.append(line)
    if chunk:
        text = "\n".join(chunk).strip()
        if text:
            elements.append({"tag": "markdown", "content": text})
    return elements or [{"tag": "markdown", "content": " "}]


def normalize_feishu_markdown_text(text: str) -> str:
    """Normalize Markdown formatting for Feishu's CardKit 2.0 / Lark-MD renderer.

    Feishu Markdown parser has a well-known delimiter issue where **bold** or *italic*
    markers directly glued to CJK characters or Chinese punctuation (e.g. `但**“文字”**属于`)
    fail to trigger emphasis and render as literal asterisks.
    Padding boundaries with CJK-safe spaces ensures 100% reliable rich text rendering.
    Protects fenced code blocks and inline code from delimiter mutation, and eliminates
    unwanted padding gaps against Chinese closing/opening punctuation.
    """
    if not text or "**" not in text:
        return text or ""

    intermediate, code_store = _protect_code(text)
    if "**" not in intermediate:
        return _restore_code(intermediate, code_store)

    # 规整 **“xxx”** 嵌套
    intermediate = re.sub(r'\*\*\s*“([^”]+)”\s*\*\*', r'“**\1**”', intermediate)

    pattern = re.compile(r'([^\s*`])?\*\*([^*\n]+?)\*\*([^\s*`])?')

    def _pad_bold(m: re.Match) -> str:
        prefix = m.group(1) or ""
        inner = m.group(2)
        suffix = m.group(3) or ""
        p_space = ""
        if prefix and not prefix.isspace():
            if prefix in NO_LEADING_SPACE_CHARS:
                inner_s = inner.lstrip()
                if inner_s and not _is_punctuation(inner_s[0]):
                    p_space = ""
                else:
                    p_space = " "
            else:
                p_space = " "
        s_space = ""
        if suffix and not suffix.isspace():
            if suffix in NO_TRAILING_SPACE_CHARS:
                inner_s = inner.rstrip()
                if inner_s and not _is_punctuation(inner_s[-1]):
                    s_space = ""
                else:
                    s_space = " "
            else:
                s_space = " "
        return f"{prefix}{p_space}**{inner}**{s_space}{suffix}"

    res = pattern.sub(_pad_bold, intermediate)
    return _restore_code(res, code_store)


def build_cardkit2_payload(
    text: str,
    *,
    is_terminal: bool = False,
    is_failed: bool = False,
    title: str = "Super Agent",
    subtitle: str = "Super Agent 企业智能助手",
    stage_text: str = "",
    elapsed: Optional[float] = None,
    service_label: str = "",
    icon_token: str = "myai_colorful",
    status_suffix: Optional[str] = None,
    color_template: Optional[str] = None,
    footer_notes: Optional[Sequence[str]] = None,
    metric_items: Optional[Sequence[Tuple[str, str]]] = None,
    hero_img_key: Optional[str] = None,
    hero_alt: str = "预览图",
) -> Dict[str, Any]:
    """Single source of truth for assembling canonical CardKit 2.0 industrial cards.

    Conforms 100% to our enterprise CardKit 2.0 specification:
      - Header: ``myai_colorful`` icon + ``subtitle`` + semantic palette
        (``wathet`` in-progress / ``indigo`` completed / ``carmine`` failed).
      - Collapsible trace: native ``collapsible_panel`` with ``grey-50`` column_set when stages exist.
      - Main content: semantic ``column_set`` block (``turquoise-50`` / ``blue-50`` / ``red-50``).
      - Table and long-form block-level whitespace layout orchestration.
      - Optional horizontal metrics bar and hero image preview.
      - Footer: ``grey-50`` ``column_set`` for timing, node, and guardrail indicators.
    """
    extracted_stage, clean_text = extract_stages_and_clean_text(text)
    explicit_stages, explicit_stage_text = extract_stages_and_clean_text(stage_text)
    combined_stage = "\n\n".join(
        s for s in [explicit_stages, explicit_stage_text, extracted_stage] if s
    ).strip()

    if not is_terminal:
        clean_text = seal_unclosed_markdown_fences(clean_text)
        combined_stage = seal_unclosed_markdown_fences(combined_stage)

    clean_text = normalize_feishu_markdown_text(clean_text)
    combined_stage = normalize_feishu_markdown_text(combined_stage)

    if is_failed:
        header_template = color_template or "carmine"
        suffix = status_suffix if status_suffix is not None else "未能完成"
        header_title = f"{title} · {suffix}" if suffix else title
        main_bg = "red-50"
    elif is_terminal:
        header_template = color_template or "indigo"
        suffix = status_suffix if status_suffix is not None else "处理完成"
        header_title = f"{title} · {suffix}" if suffix else title
        main_bg = "blue-50"
    else:
        header_template = color_template or "wathet"
        suffix = status_suffix if status_suffix is not None else "处理中"
        header_title = f"{title} · {suffix}" if suffix else title
        main_bg = "turquoise-50"

    elements: List[Dict[str, Any]] = []

    # 1. Collapsible inspection & reasoning panel
    if combined_stage:
        panel_state = "已折叠" if is_terminal else "进行中"
        elements.append({
            "tag": "collapsible_panel",
            "expanded": not is_terminal,
            "header": {
                "tag": "markdown",
                "content": f"**排查分析链路 [{panel_state}]**",
            },
            "elements": [
                {
                    "tag": "column_set",
                    "flex_mode": "none",
                    "background_style": "grey-50",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "background_style": "grey-50",
                            "elements": [{"tag": "markdown", "content": combined_stage}],
                        }
                    ],
                }
            ],
        })

    # 2. Main content block(s)
    card_blocks = build_card_elements(clean_text)
    has_table = any(b.get("tag") == "table" for b in card_blocks)

    def _make_main_box(content: str) -> Dict[str, Any]:
        return {
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": main_bg,
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "vertical_align": "top",
                    "background_style": main_bg,
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": content or " ",
                        }
                    ],
                }
            ],
        }

    def _first_unfenced_divider_index(lines: List[str]) -> int:
        fenced = _fenced_line_flags(lines)
        for idx, (line, is_fenced) in enumerate(zip(lines, fenced)):
            if not is_fenced and _is_standalone_divider(line):
                return idx
        return -1

    if has_table:
        first_table_idx = next(i for i, b in enumerate(card_blocks) if b.get("tag") == "table")
        if first_table_idx > 0:
            first_text = card_blocks[0].get("content", "").strip()
            if first_text:
                elements.append(_make_main_box(first_text))
            subsequent = card_blocks[1:]
        else:
            subsequent = card_blocks

        for block in subsequent:
            if block.get("tag") == "table":
                elements.append(block)
            elif block.get("tag") == "markdown":
                elements.extend(_split_markdown_by_dividers(block.get("content", "")))
            else:
                elements.append(block)

    elif len(clean_text) >= 600:
        lines = clean_text.splitlines()
        first_div_idx = _first_unfenced_divider_index(lines)
        if first_div_idx != -1:
            first_part = "\n".join(lines[:first_div_idx]).strip()
            rest = "\n".join(lines[first_div_idx + 1:]).strip()
            if first_part:
                elements.append(_make_main_box(first_part))
            elements.append({"tag": "hr"})
            if rest:
                elements.extend(_split_markdown_by_dividers(rest))
        else:
            elements.append(_make_main_box(clean_text))

    else:
        elements.append(_make_main_box(clean_text))

    # Metric items horizontal column_set (rendered after main content blocks, before hero/footer)
    if metric_items:
        col_count = len(metric_items)
        if col_count == 2:
            flex_mode = "bisect"
        elif col_count == 3:
            flex_mode = "trisect"
        else:
            flex_mode = "stretch"

        columns = [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "background_style": "grey-50",
                "elements": [
                    {
                        "tag": "markdown",
                        "content": f"**{label}**\n{val}",
                    }
                ],
            }
            for label, val in metric_items
        ]
        elements.append({
            "tag": "column_set",
            "flex_mode": flex_mode,
            "background_style": "grey-50",
            "columns": columns,
        })

    # Hero image preview component (rendered before footer)
    if hero_img_key and hero_img_key.strip():
        elements.append({
            "tag": "img",
            "img_key": hero_img_key.strip(),
            "preview": True,
            "alt": {
                "tag": "plain_text",
                "content": hero_alt or "预览图",
            },
        })

    # 3. Footer / Note indicators (grey-50 block)
    footer_items: List[str] = []
    if elapsed is not None and elapsed > 0:
        footer_items.append(f"耗时: {elapsed:.1f}s")
    if service_label:
        footer_items.append(f"执行节点: {service_label}")
    if footer_notes:
        for note_item in footer_notes:
            if note_item and note_item not in footer_items:
                footer_items.append(note_item)
    if footer_items:
        elements.append({
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": "grey-50",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "vertical_align": "top",
                    "background_style": "grey-50",
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": "<font color='grey'>" + "  |  ".join(footer_items) + "</font>",
                        }
                    ],
                }
            ],
        })

    header_block: Dict[str, Any] = {
        "title": {"tag": "plain_text", "content": header_title},
        "template": header_template,
    }
    if subtitle:
        header_block["subtitle"] = {"tag": "plain_text", "content": subtitle}
    if icon_token:
        header_block["icon"] = {"tag": "standard_icon", "token": icon_token}

    return {
        "schema": "2.0",
        "config": {
            "wide_screen_mode": True,
            "update_multi": True,
        },
        "header": header_block,
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 12px 12px",
            "elements": elements,
        },
    }


def build_streaming_progress_body(
    *,
    action: str,
    elapsed: int,
    tool_n: int = 0,
    timeout: float = 0.0,
    say: str = "",
    stage: str = "",
    tick: int = 0,
    is_error: bool = False,
    tip: str = "",
) -> str:
    """Format single markdown body frame for streaming progress (pure engineering aesthetics, zero-emoji)."""
    head = stage.strip() or action.strip() or "正在处理"

    if is_error:
        return f"{INDICATOR_ERROR} **{head}**\n{DIVIDER}\n已用 {elapsed} 秒"

    color = color_for_tick(tick)
    title = f'<font color="{color}">**{INDICATOR_ACTIVE} {head}**</font>'

    left = int(timeout - elapsed) if timeout and timeout > 0 else None
    if left is not None and left <= 120:
        left = max(0, left)
        return (
            f"**{INDICATOR_WARN} 即将超时，还剩 {left} 秒**\n"
            f"发送「取消」可提前停止\n"
            f"{DIVIDER}\n"
            f"{INDICATOR_TIMER} {elapsed}s · {INDICATOR_STEP} {tool_n}"
        )

    parts = [title]
    if say:
        parts.append(say.strip())

    meta = f"{DIVIDER}\n{INDICATOR_TIMER} **{elapsed}s**"
    if tool_n > 0:
        meta += f" · {INDICATOR_STEP} **{tool_n}**"
    parts.append(meta)

    if tip:
        parts.append(f"{DIVIDER}\n{tip}")

    return "\n".join(parts)


class CardStreamRenderer:
    """Single-inflight asynchronous progress card renderer.

    Guarantees:
      1. Non-blocking callers (note/render are synchronous or lightweight async).
      2. Exactly one in-flight PATCH call at any given moment.
      3. Last-Write-Wins coalescing of pending frames, not a platform rate limit.
      4. ``close(); await drain()`` settles client-side progress calls before
         final delivery. Timeout/failure must be reconciled by the transport;
         a local drain cannot prove remote ordering or delivery.
    """

    def __init__(
        self,
        card_id: str,
        patch_fn: Callable[[str, Dict[str, Any]], Awaitable[None]],
        *,
        title: str = "Super Agent",
        subtitle: str = "Autonomous Engineering Assistant",
        service_label: str = "",
        timeout: float = 0.0,
        clock: Optional[Callable[[], float]] = None,
    ):
        self.card_id = card_id
        self._patch_fn = patch_fn
        self.title = title
        self.subtitle = subtitle
        self.service_label = service_label
        self.timeout = timeout
        self._clock = clock or time.monotonic
        self.start_time = float(self._clock())

        self.closed = False
        self.action = "正在思考…"
        self.stage = ""
        self.say = ""
        self.tool_n = 0
        self.tip = ""
        self._tick = 0

        self.last_rendered_body = ""
        self._inflight: Optional[asyncio.Task[None]] = None
        self._pending_payload: Optional[Dict[str, Any]] = None
        self._patch_failed = False

    def update_stage(self, stage: str) -> None:
        """Update stage and trigger a render frame."""
        if self.closed:
            return
        if stage and stage != self.stage:
            self.stage = stage
            self.render()

    def update_action(self, action: str, *, tool_name: str = "") -> None:
        """Update tool action or text statement."""
        if self.closed:
            return
        if tool_name:
            self.tool_n += 1
        if action:
            self.action = action
        self.render()

    def set_tech_tip(self, tip: str) -> None:
        """Inject educational or status tip."""
        if self.closed:
            return
        self.tip = tip
        self.render()

    def render(self) -> None:
        """Evaluate current frame and dispatch to the background pump."""
        if self.closed:
            return
        elapsed = int(self._clock() - self.start_time)
        body = build_streaming_progress_body(
            action=self.action,
            elapsed=elapsed,
            tool_n=self.tool_n,
            timeout=self.timeout,
            say=self.say,
            stage=self.stage,
            tick=self._tick,
            tip=self.tip,
        )
        self._tick += 1
        if body != self.last_rendered_body:
            self.last_rendered_body = body
            payload = build_cardkit2_payload(
                body,
                is_terminal=False,
                is_failed=False,
                title=self.title,
                subtitle=self.subtitle,
                elapsed=float(elapsed),
                service_label=self.service_label,
            )
            self._dispatch(payload)

    def _dispatch(self, payload: Dict[str, Any]) -> None:
        if self.closed:
            return
        self._pending_payload = payload
        self._pump()

    def _pump(self) -> None:
        if self.closed:
            self._pending_payload = None
            return
        if self._inflight is not None or self._pending_payload is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        payload = self._pending_payload
        self._pending_payload = None
        self._inflight = loop.create_task(self._send(payload))

    async def _send(self, payload: Dict[str, Any]) -> None:
        try:
            if not self.closed:
                await self._patch_fn(self.card_id, payload)
        except asyncio.CancelledError:
            self._patch_failed = True
            raise
        except Exception as exc:
            self._patch_failed = True
            # Transport exceptions may contain card content, URLs or secrets.
            logger.warning("Progress card update error: %s", type(exc).__name__)
        finally:
            self._inflight = None
            self._pump()

    def close(self) -> None:
        """Close the card: stops new progress updates from dispatching."""
        self.closed = True
        self._pending_payload = None

    async def drain(self, budget: float = 10.0) -> None:
        """Wait for progress calls, or raise without discarding a live handle.

        Close admission first when handing off to final delivery. A timeout
        deliberately leaves the in-flight operation observable and does not
        cancel it: cancellation cannot undo a request already sent remotely.
        """
        if (isinstance(budget, bool) or not isinstance(budget, (int, float))
                or not math.isfinite(budget) or budget <= 0):
            raise ValueError("drain budget must be finite and positive")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget
        while True:
            # A task cancelled before its coroutine starts never executes
            # _send's finally block. Retire only a proven terminal handle.
            if self._inflight is not None and self._inflight.done():
                if self._inflight.cancelled() or self._inflight.exception() is not None:
                    self._patch_failed = True
                self._inflight = None
            self._pump()
            task = self._inflight
            if task is None and self._pending_payload is None:
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("progress_card_drain_timeout")
            if task is not None:
                done, _ = await asyncio.wait({task}, timeout=remaining)
                if not done:
                    raise TimeoutError("progress_card_drain_timeout")
        if self._patch_failed:
            raise RuntimeError("progress_card_update_failed")

