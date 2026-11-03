"""agentcore.cardkit2_renderer — Native CardKit 2.0 streaming progress & final delivery renderer.

Ported from proven production patterns (single-inflight async pipeline + CardKit 2.0
semantic background palettes + native collapsible stage panels). Completely decoupled
from external sidecars (HFC) and business logic.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

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


def build_cardkit2_payload(
    text: str,
    *,
    is_terminal: bool = False,
    is_failed: bool = False,
    title: str = "Super Agent",
    subtitle: str = "Autonomous Engineering Assistant",
    stage_text: str = "",
    elapsed: Optional[float] = None,
    service_label: str = "",
    icon_token: str = "myai_colorful",
) -> Dict[str, Any]:
    """Assemble a standard Feishu CardKit 2.0 JSON payload.

    Applies enterprise color palette:
      - Processing (in-progress): header 'wathet', main content 'turquoise-50'
      - Completed (terminal success): header 'indigo', main content 'blue-50'
      - Failed (terminal error): header 'carmine', main content 'red-50'
      - Inspection trace / stages: 'grey-50' in native collapsible panel
      - Footer metadata: 'grey-50'
    """
    extracted_stage, clean_text = extract_stages_and_clean_text(text)
    explicit_stages, explicit_stage_text = extract_stages_and_clean_text(stage_text)
    combined_stage = "\n\n".join(
        s for s in [explicit_stages, explicit_stage_text, extracted_stage] if s
    ).strip()

    if is_failed:
        header_template = "carmine"
        header_title = f"{title} · 未能完成"
        main_bg = "red-50"
    elif is_terminal:
        header_template = "indigo"
        header_title = f"{title} · 处理完成"
        main_bg = "blue-50"
    else:
        header_template = "wathet"
        header_title = f"{title} · 处理中"
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

    # 2. Main content block
    elements.append({
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
                        "content": clean_text or " ",
                    }
                ],
            }
        ],
    })

    # 3. Footer metadata block
    footer_items: List[str] = []
    if elapsed is not None and elapsed > 0:
        footer_items.append(f"耗时: {elapsed:.1f}s")
    if service_label:
        footer_items.append(f"执行节点: {service_label}")

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
                            "content": "  |  ".join(footer_items),
                        }
                    ],
                }
            ],
        })

    payload: Dict[str, Any] = {
        "schema": "2.0",
        "config": {
            "wide_screen_mode": True,
            "update_multi": True,
        },
        "header": {
            "title": {"tag": "plain_text", "content": header_title},
            "subtitle": {"tag": "plain_text", "content": subtitle},
            "template": header_template,
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 12px 12px",
            "elements": elements,
        },
    }

    if icon_token:
        payload["header"]["icon"] = {
            "tag": "standard_icon",
            "token": icon_token,
        }

    return payload


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

