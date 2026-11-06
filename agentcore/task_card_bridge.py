"""agentcore.task_card_bridge — Decoupled task-bound lifecycle card bridge.

Binds messaging cards strictly to durable task IDs, decoupling card state from
transient frontdesk conversation turns. Provides in-place card updates across
the task's entire lifecycle (queued -> running -> progressive delivery -> terminal).
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Zero-emoji engineering indicators conforming to Super Agent UX discipline
STATUS_LABELS = {
    "queued": "排队中",
    "running": "执行中",
    "progressive": "渐进交付中",
    "completed": "已完成",
    "failed": "执行失败",
    "cancelled": "已取消",
}

STATUS_HEADERS = {
    "queued": ("wathet", "任务状态 · 排队中"),
    "running": ("wathet", "任务状态 · 执行中"),
    "progressive": ("blue", "任务状态 · 渐进交付中"),
    "completed": ("green", "任务状态 · 顺利完成"),
    "failed": ("carmine", "任务状态 · 执行未通过"),
    "cancelled": ("grey", "任务状态 · 已取消"),
}


def _render_progress_bar(progress: float, width: int = 12) -> str:
    """Render a clean text-based progress bar."""
    p = max(0.0, min(1.0, float(progress)))
    filled = int(round(p * width))
    empty = width - filled
    return f"[{'■' * filled}{'□' * empty}]"


class TaskCardBridge:
    """Manages the decoupled lifecycle of task-bound cards."""

    def __init__(self, root: Path | str, *, profile: str, sender: Optional[Any] = None,
                 min_update_interval: float = 1.2):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.profile = profile
        self.sender = sender
        self.min_update_interval = float(min_update_interval)
        self._last_patched: Dict[str, float] = {}
        self._init_db()

    def _init_db(self) -> None:
        with self.db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_cards (
                    task_id TEXT PRIMARY KEY,
                    profile TEXT NOT NULL,
                    route TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    message_id TEXT,
                    role TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    phase TEXT,
                    progress REAL DEFAULT 0.0,
                    elapsed REAL DEFAULT 0.0,
                    summary TEXT,
                    artifacts_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    finalized INTEGER NOT NULL DEFAULT 0
                )
            """)

    @contextmanager
    def db(self):
        db_path = self.root / "task_cards.db"
        conn = sqlite3.connect(db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def build_card_payload(
        self,
        task_id: str,
        role_name: str,
        goal: str,
        status: str,
        *,
        phase: str = "",
        progress: float = 0.0,
        elapsed: float = 0.0,
        summary: str = "",
        artifacts: Optional[List[str]] = None,
        error_msg: str = "",
    ) -> Dict[str, Any]:
        """Construct a clean, responsive CardKit 2.0 payload bound to the task."""
        template, title_text = STATUS_HEADERS.get(status, ("wathet", f"任务状态 · {status}"))
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")

        # Trim goal for clean card presentation
        clean_goal = (goal or "").strip().replace("\r\n", "\n")
        if len(clean_goal) > 280:
            clean_goal = clean_goal[:277] + "..."

        body_lines = [
            f"**任务编号**：`{task_id}`",
            f"**指派角色**：{role_name}",
            f"**任务目标**：{clean_goal}",
        ]

        if status in ("queued", "running", "progressive"):
            body_lines.append("────────────────")
            if phase:
                body_lines.append(f"**当前阶段**：{phase}")
            if progress > 0.0:
                bar = _render_progress_bar(progress)
                body_lines.append(f"**阶段进度**：{bar} {int(progress * 100)}%")
            if elapsed > 0.0:
                body_lines.append(f"**执行耗时**：{elapsed:.1f} 秒")
            if status == "queued":
                body_lines.append("ℹ️ 等待后台 Worker 队列空闲调度...")
            else:
                body_lines.append("⚡ 后台 Worker 正在全速执行中，文字结论将优先直达。")

        elif status == "completed":
            body_lines.append("────────────────")
            if elapsed > 0.0:
                body_lines.append(f"**执行耗时**：{elapsed:.1f} 秒")
            if summary:
                body_lines.append(f"**核心结论（Phase 1 直达）**：\n{summary.strip()}")
            if artifacts:
                body_lines.append("**产出制品（Phase 2）**：")
                for art in artifacts:
                    body_lines.append(f"- `{Path(art).name}`: {art}")

        elif status in ("failed", "cancelled"):
            body_lines.append("────────────────")
            if elapsed > 0.0:
                body_lines.append(f"**执行耗时**：{elapsed:.1f} 秒")
            fail_desc = error_msg or summary or "任务未达到预期终态或已取消"
            body_lines.append(f"**未完成说明**：\n{fail_desc}")

        body_lines.append("")
        body_lines.append(f"<font color='grey'>状态更新于 {now_str} · 卡片与任务严格解耦绑定</font>")

        elements = [
            {
                "tag": "markdown",
                "content": "\n".join(body_lines),
            },
        ]

        return {
            "schema": "2.0",
            "header": {
                "template": template,
                "title": {
                    "tag": "plain_text",
                    "content": title_text,
                },
            },
            "body": {
                "elements": elements,
            },
        }

    def bind_task(
        self,
        task_id: str,
        origin: Dict[str, Any],
        goal: str,
        role: str,
        display_name: str,
        message_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record task-to-card binding metadata."""
        now = time.time()
        route = str(origin.get("route") or "")
        chat_id = str(origin.get("chat") or "")
        channel = str(origin.get("channel") or "feishu")

        with self.db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO task_cards (
                    task_id, profile, route, chat_id, channel, message_id,
                    role, display_name, goal, status, phase, progress, elapsed,
                    summary, artifacts_json, created_at, updated_at, finalized
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', '已提交排队', 0.0, 0.0, NULL, NULL, ?, ?, 0)
            """, (task_id, self.profile, route, chat_id, channel, message_id,
                  role, display_name, goal, now, now))

        card_payload = self.build_card_payload(
            task_id, display_name, goal, "queued", phase="已提交排队"
        )
        return {"ok": True, "task_id": task_id, "message_id": message_id, "card_payload": card_payload}

    def set_message_id(self, task_id: str, message_id: str) -> None:
        """Associate a real messaging platform message_id once emitted."""
        with self.db() as conn:
            conn.execute("UPDATE task_cards SET message_id=?, updated_at=? WHERE task_id=?",
                         (message_id, time.time(), task_id))

    def update_progress(
        self,
        task_id: str,
        *,
        status: str = "running",
        phase: str = "",
        progress: float = 0.0,
        elapsed: float = 0.0,
        force_sync: bool = False,
    ) -> Dict[str, Any]:
        """Update live execution progress and patch the card in place."""
        now = time.time()
        last_t = self._last_patched.get(task_id, 0.0)
        if not force_sync and (now - last_t) < self.min_update_interval:
            return {"ok": True, "skipped_throttle": True}

        with self.db() as conn:
            row = conn.execute(
                "SELECT role, display_name, goal, message_id, route, chat_id, channel FROM task_cards WHERE task_id=?",
                (task_id,)
            ).fetchone()
            if not row:
                return {"ok": False, "error": "task_not_found"}

            conn.execute("""
                UPDATE task_cards
                SET status=?, phase=?, progress=?, elapsed=?, updated_at=?
                WHERE task_id=?
            """, (status, phase, progress, elapsed, now, task_id))

        role, display_name, goal, message_id, route, chat_id, channel = row
        card_payload = self.build_card_payload(
            task_id, display_name, goal, status, phase=phase, progress=progress, elapsed=elapsed
        )

        patch_result = None
        if message_id and self.sender and callable(self.sender):
            try:
                patch_result = self.sender(
                    address={"route": route, "chat": chat_id, "channel": channel, "profile": self.profile},
                    message_id=message_id,
                    card_payload=card_payload,
                )
                self._last_patched[task_id] = now
            except Exception as e:
                logger.warning("TaskCardBridge patch failed for %s: %s", task_id, e)
                patch_result = {"ok": False, "error": str(e)}

        return {"ok": True, "task_id": task_id, "card_payload": card_payload, "patch_result": patch_result}

    def finalize(
        self,
        task_id: str,
        *,
        status: str = "completed",
        summary: str = "",
        artifacts: Optional[List[str]] = None,
        elapsed: float = 0.0,
        error_msg: str = "",
    ) -> Dict[str, Any]:
        """Finalize the card upon terminal outcome (turn-green contract)."""
        now = time.time()
        with self.db() as conn:
            row = conn.execute(
                "SELECT role, display_name, goal, message_id, route, chat_id, channel FROM task_cards WHERE task_id=?",
                (task_id,)
            ).fetchone()
            if not row:
                return {"ok": False, "error": "task_not_found"}

            conn.execute("""
                UPDATE task_cards
                SET status=?, summary=?, artifacts_json=?, elapsed=?, updated_at=?, finalized=1
                WHERE task_id=?
            """, (status, summary, json.dumps(artifacts or []), elapsed, now, task_id))

        role, display_name, goal, message_id, route, chat_id, channel = row
        card_payload = self.build_card_payload(
            task_id, display_name, goal, status, elapsed=elapsed,
            summary=summary, artifacts=artifacts, error_msg=error_msg
        )

        patch_result = None
        if message_id and self.sender and callable(self.sender):
            try:
                patch_result = self.sender(
                    address={"route": route, "chat": chat_id, "channel": channel, "profile": self.profile},
                    message_id=message_id,
                    card_payload=card_payload,
                )
                self._last_patched[task_id] = now
            except Exception as e:
                logger.warning("TaskCardBridge finalize patch failed for %s: %s", task_id, e)
                patch_result = {"ok": False, "error": str(e)}

        return {"ok": True, "task_id": task_id, "finalized": True, "card_payload": card_payload, "patch_result": patch_result}

    def get_card(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Query stored card status and metadata."""
        with self.db() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM task_cards WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                return None
            return dict(row)
