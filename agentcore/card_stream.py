"""agentcore.card_stream — Card 2.4 streaming progress cards for autonomous dev pipelines.

Builds structured, monotonic Feishu Card 2.4 (card_components v2) payloads for multi-stage
engineering jobs. Ensures `config.update_multi: true` for in-place PATCH frame updates,
uses standard pure-text indicators ([OK], [RUNNING], [WAIT], [FAIL]) conforming to zero-emoji
specifications, and provides bidirectional synchronization with agentcore.liveness.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

DEFAULT_STAGES = [
    ("analysis", "需求规格与因果链拆解"),
    ("cases", "状态机条件与用例生成"),
    ("review", "红队 6 维度代码审查"),
    ("verify", "静态 AST 与门禁验证"),
]

STAGE_STATUS_INDICATORS = {
    "waiting": "[WAIT]",
    "running": "[RUNNING]",
    "passed": "[OK]",
    "failed": "[FAIL]",
    "skipped": "[SKIP]",
}

HEADER_TEMPLATES = {
    "queued": "grey",
    "running": "blue",
    "done": "green",
    "failed": "red",
    "cancelled": "grey",
}


class CardStreamBuilder:
    """Builds and advances Card 2.4 streaming progress updates for dev workflows."""

    def __init__(
        self,
        task_id: str,
        title: str = "自主研发任务流水线",
        stages: Optional[Sequence[Tuple[str, str]]] = None,
        clock: Optional[Any] = None,
    ):
        self.task_id = task_id
        self.title = title
        self._clock = clock or time.time
        self.created_at = float(self._clock())
        self.updated_at = self.created_at
        self.state = "queued"
        self.detail = ""
        self.metrics: Dict[str, Any] = {}
        self.artifacts: List[str] = []

        raw_stages = stages or DEFAULT_STAGES
        self.stages: Dict[str, Dict[str, Any]] = {}
        for key, name in raw_stages:
            self.stages[key] = {
                "key": key,
                "name": name,
                "status": "waiting",
                "note": "",
                "updated_at": self.created_at,
            }

    def _now(self) -> float:
        return float(self._clock())

    def advance(
        self,
        stage_key: str,
        status: str = "running",
        note: str = "",
        state: Optional[str] = None,
        detail: str = "",
    ) -> CardStreamBuilder:
        """Advance a pipeline stage status and update card lifecycle."""
        if stage_key not in self.stages:
            raise ValueError(f"unknown_stage: {stage_key}. Expected one of {list(self.stages)}")
        if status not in STAGE_STATUS_INDICATORS:
            raise ValueError(f"unknown_status: {status}. Expected one of {list(STAGE_STATUS_INDICATORS)}")

        now = self._now()
        self.stages[stage_key]["status"] = status
        self.stages[stage_key]["note"] = note
        self.stages[stage_key]["updated_at"] = now
        self.updated_at = now

        if state:
            self.state = state
        elif status == "running" and self.state == "queued":
            self.state = "running"
        elif status == "failed":
            self.state = "failed"

        if detail:
            self.detail = detail

        return self

    def complete(self, success: bool = True, detail: str = "") -> CardStreamBuilder:
        """Mark the card in its terminal state."""
        self.state = "done" if success else "failed"
        self.updated_at = self._now()
        if detail:
            self.detail = detail
        return self

    def add_artifact(self, artifact_path: str) -> CardStreamBuilder:
        if artifact_path and artifact_path not in self.artifacts:
            self.artifacts.append(artifact_path)
        return self

    def set_metric(self, key: str, value: Any) -> CardStreamBuilder:
        self.metrics[key] = value
        return self

    def sync_from_liveness(self, live: Dict[str, Any]) -> CardStreamBuilder:
        """Update card state from an agentcore.liveness inspection payload."""
        if not isinstance(live, dict) or not live.get("ok"):
            return self

        state = live.get("state") or self.state
        if state in ("queued", "running", "done", "failed", "cancelled"):
            self.state = state

        if live.get("detail"):
            self.detail = str(live.get("detail"))

        phase = live.get("phase", "")
        if phase and phase in self.stages:
            self.advance(phase, status="running", state=self.state)

        for art in live.get("artifacts") or []:
            self.add_artifact(str(art))

        return self

    def render_card(self) -> Dict[str, Any]:
        """Render Feishu Card 2.4 compliant JSON payload."""
        elapsed_s = max(0, int(self.updated_at - self.created_at))
        minutes, seconds = divmod(elapsed_s, 60)
        time_str = f"{minutes}分{seconds:02d}秒" if minutes else f"{seconds}秒"

        header_color = HEADER_TEMPLATES.get(self.state, "blue")
        status_label = {
            "queued": "[排队中]",
            "running": "[执行中]",
            "done": "[已完成]",
            "failed": "[失败]",
            "cancelled": "[已取消]",
        }.get(self.state, f"[{self.state}]")

        # Stage list rows
        stage_lines = []
        for stage in self.stages.values():
            ind = STAGE_STATUS_INDICATORS.get(stage["status"], "[WAIT]")
            line = f"{ind} {stage['name']}"
            if stage["note"]:
                line += f" ({stage['note']})"
            stage_lines.append(line)

        elements: List[Dict[str, Any]] = [
            {
                "tag": "div",
                "text": {
                    "tag": "plain_text",
                    "content": f"任务 ID: {self.task_id} | 耗时: {time_str}\n\n" + "\n".join(stage_lines),
                },
            }
        ]

        if self.detail:
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "plain_text",
                    "content": f"说明: {self.detail}",
                },
            })

        if self.artifacts:
            art_text = "生成产物:\n" + "\n".join(f"- {a}" for a in self.artifacts[:5])
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "plain_text",
                    "content": art_text,
                },
            })

        # Actions
        actions: List[Dict[str, Any]] = []
        if self.state in ("queued", "running"):
            actions.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "取消任务"},
                "type": "danger",
                "value": {"action": "cancel", "task_id": self.task_id},
            })
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "查看状态"},
            "type": "default",
            "value": {"action": "status", "task_id": self.task_id},
        })
        if self.state == "done":
            actions.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "验收报告"},
                "type": "primary",
                "value": {"action": "report", "task_id": self.task_id},
            })

        elements.append({
            "tag": "action",
            "actions": actions,
        })

        return {
            "config": {
                "wide_screen_mode": True,
                "update_multi": True,
            },
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"{status_label} {self.title}",
                },
                "template": header_color,
            },
            "elements": elements,
        }

    def render_patch_payload(self) -> Dict[str, Any]:
        """Wrap card payload for an in-place message edit / PATCH call."""
        card_dict = self.render_card()
        return {
            "msg_type": "interactive",
            "card": card_dict,
        }

