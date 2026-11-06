"""Progressive Delivery (渐进式交付) protocol and execution coordinator.

Core Architecture
-----------------
Decouples fast textual analysis/report completion delivery (Phase 1) from heavy
out-of-band browser/diagram rendering (Phase 2):

[Agent analysis complete]
       │
       ├─► ① Immediately Deliver: core text conclusion + Markdown report (0-blocking)
       │
       └─► ② Trigger Out-of-band: background browser / diagram rendering
                 │
                 └─► ③ Rendering ready: card update / attachment appended
                       (or graceful degradation on timeout / failure)

Guarantees:
* Phase 1 text delivery is never withheld, blocked, or delayed by Phase 2 rendering.
* Checkpoint output is safely persisted on disk (checkpoint.json + report.md).
* Out-of-band rendering failure or timeout degrades gracefully without failing the job.
"""
from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: Markers that indicate a Phase 1 progressive delivery or checkpoint output.
PROGRESSIVE_MARKERS = re.compile(
    r"(?m)^\s*##(?:CHECKPOINT|PHASE1|PROGRESSIVE_DELIVERY):\s*\S+"
    r"|\[(?:CHECKPOINT|PROGRESSIVE[ _-]DELIVERY|渐进式交付)\]"
    r"|出图作为异步附加物|后台渲染中|渲染中\.\.\.|rendering in background|out-of-band rendering",
    re.I,
)

MAX_CHECKPOINT_CHARS = 12000
MAX_SUMMARY_CHARS = 3500


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


@dataclass(frozen=True)
class Checkpoint:
    """Immutable checkpoint record for Phase 1 text delivery."""

    job_id: str
    attempt: str
    phase: str
    summary: str
    report_md: str = ""
    artifacts: Tuple[str, ...] = ()
    rendering_state: str = "pending"  # pending | rendering | completed | degraded | skipped | failed
    timestamp: float = 0.0
    error: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "job": self.job_id,
            "attempt": self.attempt,
            "phase": self.phase,
            "summary": self.summary,
            "artifacts": list(self.artifacts),
            "rendering_state": self.rendering_state,
            "timestamp": self.timestamp,
        }
        if self.report_md:
            result["report_md"] = self.report_md
        if self.error:
            result["error"] = self.error
        if self.extra:
            result["extra"] = self.extra
        return result


def is_progressive_phase1(text: Optional[str]) -> bool:
    """Whether response text represents a Phase 1 progressive delivery or checkpoint."""
    if not isinstance(text, str) or not text.strip():
        return False
    return PROGRESSIVE_MARKERS.search(text) is not None


def save_checkpoint(
    directory: Path | str,
    *,
    job_id: str,
    attempt: str,
    summary: str,
    report_md: str = "",
    artifacts: Sequence[str] = (),
    phase: str = "phase1_text",
    rendering_state: str = "pending",
    error: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Persist a Phase 1 checkpoint and report to disk atomically."""
    target_dir = Path(directory)
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    now = time.time()
    bounded_summary = (summary or "").strip()[:MAX_CHECKPOINT_CHARS]

    record = Checkpoint(
        job_id=str(job_id),
        attempt=str(attempt),
        phase=phase,
        summary=bounded_summary,
        report_md=report_md,
        artifacts=tuple(str(a) for a in artifacts),
        rendering_state=rendering_state,
        timestamp=now,
        error=error,
        extra=dict(extra or {}),
    )
    data = record.to_dict()
    _atomic_write(target_dir / "checkpoint.json", json.dumps(data, ensure_ascii=False, indent=2))
    if report_md:
        _atomic_write(target_dir / "report.md", report_md)
    return data


def load_checkpoint(directory: Path | str) -> Optional[Dict[str, Any]]:
    """Read a checkpoint.json record from an attempt directory, if present."""
    path = Path(directory) / "checkpoint.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("job") and data.get("attempt"):
            return data
    except (OSError, ValueError):
        pass
    return None


def format_progressive_message(
    summary: str,
    *,
    checkpoint_id: str = "",
    rendering: bool = True,
) -> str:
    """Format a response message declaring Phase 1 text delivery and out-of-band rendering."""
    lines = [
        "[渐进式交付] 阶段一：核心文字结论与结构化报告已就绪。",
    ]
    if checkpoint_id:
        lines.append(f"##CHECKPOINT: {checkpoint_id}")
    if rendering:
        lines.append("出图作为异步附加物：图表与文档渲染在后台旁路进行中，完成后自动原地刷新卡片。")
    lines.append("")
    lines.append((summary or "").strip())
    return "\n".join(lines)


def build_progressive_receipt(
    job_id: str,
    attempt: str,
    summary: str,
    *,
    artifacts: Sequence[str] = (),
    rendering_state: str = "completed",
    degraded: bool = False,
    degradation_reason: Optional[str] = None,
    verification: str = "progressive_delivery_v1",
) -> Dict[str, Any]:
    """Construct an agentcore queue receipt representing progressive delivery."""
    result_data = {
        "job": job_id,
        "attempt": attempt,
        "summary": (summary or "").strip()[:MAX_SUMMARY_CHARS],
        "artifacts": list(artifacts),
        "rendering_state": rendering_state,
        "phase": "phase1_text" if rendering_state in ("pending", "rendering") else "completed",
        "checkpoint_delivered": True,
        "verification": verification,
        "degraded": degraded,
    }
    if degradation_reason:
        result_data["degradation_reason"] = degradation_reason

    return {
        "ok": True,
        "result": result_data,
        "degraded": degraded,
        "degradation_reason": degradation_reason,
    }


class ProgressiveDeliveryCoordinator:
    """Coordinates Phase 1 immediate text delivery and Phase 2 out-of-band rendering.

    Accepts an optional CardEmitter or card handle to update interactive cards in place.
    """

    def __init__(
        self,
        *,
        card_emitter: Optional[Any] = None,
        transport: Optional[Any] = None,
        clock: Optional[Callable[[], float]] = None,
    ):
        self.card_emitter = card_emitter
        self.transport = transport
        self.clock = clock or time.monotonic

    def deliver_phase1(
        self,
        job_id: str,
        summary: str,
        *,
        artifacts: Sequence[str] = (),
        detail: str = "文字结论已交付；正在后台渲染图表...",
    ) -> Dict[str, Any]:
        """Deliver Phase 1 text conclusion immediately without blocking on rendering."""
        if self.card_emitter is not None:
            try:
                # Update card in place to indicate text is delivered and rendering is in progress.
                self.card_emitter.update(
                    phase="rendering",
                    detail=detail,
                    artifacts=artifacts,
                )
            except Exception as exc:
                logger.warning("Failed to update card for phase 1: %s", type(exc).__name__)

        return {
            "ok": True,
            "phase": "phase1_text",
            "summary": summary,
            "artifacts": list(artifacts),
            "rendering_state": "rendering",
        }

    def run_out_of_band_rendering(
        self,
        render_fn: Callable[[], Sequence[str]],
        *,
        timeout_s: float = 60.0,
        base_artifacts: Sequence[str] = (),
        on_success: Optional[Callable[[Sequence[str]], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> Dict[str, Any]:
        """Execute Phase 2 rendering out-of-band with strict timeout and error containment.

        Guarantees that rendering crashes or timeouts never raise or block callers,
        and gracefully finishes the card emitter without destroying Phase 1 delivery.
        """
        all_artifacts = list(base_artifacts)
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(render_fn)
        try:
            rendered = future.result(timeout=max(0.1, float(timeout_s)))
            if rendered:
                for item in rendered:
                    if str(item) not in all_artifacts:
                        all_artifacts.append(str(item))

            if self.card_emitter is not None:
                try:
                    self.card_emitter.finish(
                        phase="completed",
                        artifacts=all_artifacts,
                    )
                except Exception as exc:
                    logger.warning("Failed to finish card on rendering success: %s", type(exc).__name__)

            if on_success:
                try:
                    on_success(all_artifacts)
                except Exception as exc:
                    logger.warning("on_success callback failed: %s", type(exc).__name__)

            return {
                "ok": True,
                "status": "completed",
                "artifacts": all_artifacts,
                "rendering_state": "completed",
            }
        except concurrent.futures.TimeoutError:
            logger.warning("Out-of-band rendering timed out after %s seconds", timeout_s)
            future.cancel()
            if self.card_emitter is not None:
                try:
                    self.card_emitter.finish(
                        phase="completed_degraded",
                        artifacts=all_artifacts,
                    )
                except Exception as exc:
                    logger.warning("Failed to finish card on timeout: %s", type(exc).__name__)

            if on_error:
                try:
                    on_error(TimeoutError(f"rendering_timeout_{timeout_s}s"))
                except Exception:
                    pass

            return {
                "ok": True,
                "status": "degraded",
                "reason": "rendering_timeout",
                "artifacts": all_artifacts,
                "rendering_state": "degraded",
            }
        except Exception as exc:
            logger.warning("Out-of-band rendering failed: %s", type(exc).__name__)
            if self.card_emitter is not None:
                try:
                    self.card_emitter.finish(
                        phase="completed_degraded",
                        artifacts=all_artifacts,
                    )
                except Exception:
                    pass

            if on_error:
                try:
                    on_error(exc)
                except Exception:
                    pass

            return {
                "ok": True,
                "status": "degraded",
                "reason": f"rendering_failed:{type(exc).__name__}",
                "artifacts": all_artifacts,
                "rendering_state": "failed",
            }
        finally:
            executor.shutdown(wait=False)
