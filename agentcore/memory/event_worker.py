#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T2 worker：把回合事件队列里的待抽取文本跑成记忆事实。

职责边界（刻意保持很窄）：
    ``runtime_bridge`` 只负责「事件不丢、不重、可恢复」；本模块负责
    「谁在什么时候处理它」—— 领取租约、调用 ``memory_trigger.run``、
    标记完成/失败、失败退避、过期租约回收。

为什么租约字段加在 ``turn_events`` 上而不是另建任务表：
    回合事件本身就是待执行的工作项，再建一张 ``analysis_jobs`` 式的表会
    让同一回合的生命周期分裂成两套状态机。这里只给既有表补消费字段，
    队列的主键（幂等键）和正文仍是唯一事实来源。

失败语义：
    - ``memory_trigger.run`` 返回 ``ok=False, retryable=True``（典型是
      ``provider_unavailable``）→ 退避后重试；
    - 明确的不可重试错误 → 立即 ``failed``；
    - 进程崩溃留下的 ``processing`` 行 → 租约过期后回收；重试预算耗尽的
      行直接判 ``failed``，不再无限占用队列。
    - 标记完成时校验 ``lease_owner``：租约已被别人回收的旧 worker 不能
      把结果覆盖掉。

所有状态迁移都是带条件的单条 UPDATE，因此多 worker 并发领取同一事件时
只有一个会成功。
"""

from __future__ import annotations

import logging
import os
import socket
import sqlite3
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from . import memory_trigger
from .config import logger_name
from .runtime_bridge import TurnEventQueue

logger = logging.getLogger(logger_name("event_worker"))

#: 默认租约时长（秒）。``run`` 里可能有一次模型调用，留足余量。
DEFAULT_LEASE_SECONDS = 120.0
#: 单事件最多尝试次数（含第一次）。预算耗尽后置 ``failed``。
DEFAULT_MAX_ATTEMPTS = 3
#: 指数退避起点与上限（秒）。
DEFAULT_BACKOFF_BASE_SECONDS = 30.0
DEFAULT_BACKOFF_MAX_SECONDS = 900.0
#: 落库错误文本上限，防止异常消息把队列撑大。
MAX_ERROR_CHARS = 2000
#: ``pending()`` 单次扫描上限，避免一次拉全表。
MAX_SCAN_LIMIT = 10000

#: worker 私有的消费字段（只增不改既有列；不改主键/正文语义）。
_LEASE_COLUMNS = (
    ("attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("lease_owner", "TEXT"),
    ("lease_until", "REAL"),
    ("next_attempt_at", "REAL NOT NULL DEFAULT 0"),
    ("last_error", "TEXT"),
    ("updated_at", "REAL NOT NULL DEFAULT 0"),
    ("processed_at", "REAL"),
)


def _event_key(event: Any) -> str:
    if not isinstance(event, dict):
        return ""
    return str(event.get("idempotency_key") or "").strip()


class MemoryEventWorker:
    """消费 ``TurnEventQueue`` 的幂等 worker。

    参数：
        queue        —— ``TurnEventQueue``；默认使用当前配置的数据目录。
        runner       —— 抽取入口，默认 ``memory_trigger.run``。签名
                        ``(user_message, assistant_reply, turn_status=...)``。
        owner        —— 租约持有者标识；默认 ``host:pid:随机后缀``。
        clock        —— 时钟注入点（测试用）；默认 ``time.time``。
        lease_seconds / max_attempts / backoff_base / backoff_max
                     —— 租约与重试预算。
    """

    def __init__(
        self,
        *,
        queue: Optional[TurnEventQueue] = None,
        runner: Optional[Callable[..., Dict[str, Any]]] = None,
        event_runner: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        owner: Optional[str] = None,
        clock: Optional[Callable[[], float]] = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS,
        backoff_max: float = DEFAULT_BACKOFF_MAX_SECONDS,
    ):
        self.queue = queue if queue is not None else TurnEventQueue()
        self._runner = runner if runner is not None else memory_trigger.run
        self._event_runner = event_runner
        self.owner = owner or "%s:%d:%s" % (
            socket.gethostname(), os.getpid(), uuid.uuid4().hex[:8])
        self.clock = clock or time.time
        self.lease_seconds = max(1.0, float(lease_seconds))
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_base = max(0.0, float(backoff_base))
        self.backoff_max = max(self.backoff_base, float(backoff_max))
        self._ensure_schema()

    # ------------------------------------------------------------------
    # 存储
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """复用队列的连接工厂（保持 WAL / busy_timeout 一致）。"""
        factory = getattr(self.queue, "_connect", None)
        if callable(factory):
            return factory()
        path = getattr(self.queue, "path", "")
        if not path:
            raise ValueError("queue exposes neither _connect() nor path")
        conn = sqlite3.connect(str(path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _ensure_schema(self) -> None:
        """幂等地补上消费字段；并发执行时忽略 duplicate column。"""
        conn = self._connect()
        try:
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(turn_events)")}
            for name, decl in _LEASE_COLUMNS:
                if name in existing:
                    continue
                try:
                    with conn:
                        conn.execute('ALTER TABLE turn_events ADD COLUMN "%s" %s' % (name, decl))
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 租约
    # ------------------------------------------------------------------

    def reclaim_stale(self, now: Optional[float] = None) -> Dict[str, int]:
        """回收过期租约：预算未用完的回 ``pending``，用完的判 ``failed``。"""
        now = float(self.clock() if now is None else now)
        conn = self._connect()
        try:
            with conn:
                exhausted = conn.execute(
                    "UPDATE turn_events SET state='failed', lease_owner=NULL,"
                    " lease_until=NULL, processed_at=?, updated_at=?,"
                    " last_error=COALESCE(last_error, 'lease_expired')"
                    " WHERE state='processing'"
                    " AND (lease_until IS NULL OR lease_until <= ?)"
                    " AND attempts >= ?",
                    (now, now, now, self.max_attempts)).rowcount
                reclaimed = conn.execute(
                    "UPDATE turn_events SET state='pending', lease_owner=NULL,"
                    " lease_until=NULL, next_attempt_at=?, updated_at=?"
                    " WHERE state='processing'"
                    " AND (lease_until IS NULL OR lease_until <= ?)"
                    " AND attempts < ?",
                    (now, now, now, self.max_attempts)).rowcount
        finally:
            conn.close()
        if reclaimed or exhausted:
            logger.warning("租约回收: reclaimed=%d failed=%d", reclaimed, exhausted)
        return {"reclaimed": int(reclaimed), "failed": int(exhausted)}

    def claim(self, limit: int = 1) -> List[Dict[str, Any]]:
        """从 ``pending()`` 中原子领取最多 *limit* 个到期事件。

        先回收过期租约；条件 UPDATE 保证并发 worker 只有一个能领到。
        ``pending()`` 不带 ``next_attempt_at``/租约信息，因此这里在领取时
        再次校验到期时间；扫描窗口逐步放大，避免排在前面的退避事件
        挡住后面已到期的事件。
        """
        limit = max(1, int(limit))
        self.reclaim_stale()
        claims: List[Dict[str, Any]] = []
        seen = set()
        scan = max(limit * 8, 50)
        while len(claims) < limit:
            events = self.queue.pending(limit=scan)
            fresh = [event for event in events
                     if _event_key(event) and _event_key(event) not in seen]
            for event in fresh:
                seen.add(_event_key(event))
                claimed = self._claim_one(event)
                if claimed is not None:
                    claims.append(claimed)
                    if len(claims) >= limit:
                        break
            if len(events) < scan or scan >= MAX_SCAN_LIMIT:
                break
            scan = min(scan * 4, MAX_SCAN_LIMIT)
        return claims

    def _claim_one(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        key = _event_key(event)
        if not key:
            return None
        now = float(self.clock())
        lease_until = now + self.lease_seconds
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE turn_events SET state='processing', lease_owner=?,"
                    " lease_until=?, attempts=attempts+1, updated_at=?"
                    " WHERE idempotency_key=? AND state='pending'"
                    " AND attempts < ?"
                    " AND (next_attempt_at IS NULL OR next_attempt_at <= ?)",
                    (self.owner, lease_until, now, key, self.max_attempts, now))
            if cur.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT attempts, lease_until FROM turn_events WHERE idempotency_key=?",
                (key,)).fetchone()
        finally:
            conn.close()
        return {
            "event": event,
            "idempotency_key": key,
            "attempts": int(row["attempts"]),
            "lease_until": float(row["lease_until"]),
            "owner": self.owner,
        }

    # ------------------------------------------------------------------
    # 状态迁移
    # ------------------------------------------------------------------

    def _mark_done(self, claim: Dict[str, Any]) -> bool:
        now = float(self.clock())
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE turn_events SET state='done', lease_owner=NULL,"
                    " lease_until=NULL, last_error=NULL, next_attempt_at=0,"
                    " updated_at=?, processed_at=?"
                    " WHERE idempotency_key=? AND state='processing' AND lease_owner=?",
                    (now, now, claim["idempotency_key"], self.owner))
        finally:
            conn.close()
        if cur.rowcount != 1:
            logger.warning("租约已失效，完成标记被丢弃: %s", claim["idempotency_key"])
            return False
        return True

    def _record_failure(self, claim: Dict[str, Any], *, error: str,
                        retryable: bool) -> Dict[str, Any]:
        attempts = int(claim["attempts"])
        now = float(self.clock())
        error = (error or "run_failed")[:MAX_ERROR_CHARS]
        if retryable and attempts < self.max_attempts:
            delay = min(self.backoff_base * (2 ** (attempts - 1)), self.backoff_max)
            state = "pending"
            next_attempt_at = now + delay
        else:
            state = "failed"
            next_attempt_at = 0.0

        conn = self._connect()
        try:
            with conn:
                if state == "pending":
                    cur = conn.execute(
                        "UPDATE turn_events SET state='pending', lease_owner=NULL,"
                        " lease_until=NULL, next_attempt_at=?, updated_at=?, last_error=?"
                        " WHERE idempotency_key=? AND state='processing' AND lease_owner=?",
                        (next_attempt_at, now, error,
                         claim["idempotency_key"], self.owner))
                else:
                    cur = conn.execute(
                        "UPDATE turn_events SET state='failed', lease_owner=NULL,"
                        " lease_until=NULL, next_attempt_at=0, updated_at=?,"
                        " processed_at=?, last_error=?"
                        " WHERE idempotency_key=? AND state='processing' AND lease_owner=?",
                        (now, now, error, claim["idempotency_key"], self.owner))
        finally:
            conn.close()
        if cur.rowcount != 1:
            logger.warning("租约已失效，失败标记被丢弃: %s", claim["idempotency_key"])
        return {
            "processed": True,
            "state": state,
            "attempts": attempts,
            "retryable": bool(retryable),
            "error": error,
            "next_attempt_at": next_attempt_at if state == "pending" else None,
            "idempotency_key": claim["idempotency_key"],
            "lease_lost": cur.rowcount != 1,
        }

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    def process_one(self) -> Dict[str, Any]:
        """领取一个事件并执行；没有可领取事件时返回 ``processed=False``。"""
        claims = self.claim(limit=1)
        if not claims:
            return {"processed": False, "reason": "no_claimable_event"}
        claim = claims[0]
        event = claim["event"]
        try:
            if self._event_runner is not None:
                result = self._event_runner(event)
            else:
                result = self._runner(
                    event.get("user_message") or "",
                    event.get("assistant_reply") or "",
                    turn_status="completed",
                )
        except Exception as exc:  # noqa: BLE001 —— 抽取后端异常按可重试处理
            logger.warning("回合抽取异常，按可重试失败处理: %s", exc)
            return self._record_failure(
                claim, error="%s: %s" % (type(exc).__name__, exc), retryable=True)

        if isinstance(result, dict) and result.get("ok") is True:
            done = self._mark_done(claim)
            return {
                "processed": True,
                "state": "done",
                "attempts": claim["attempts"],
                "idempotency_key": claim["idempotency_key"],
                "lease_lost": not done,
                "result": result,
            }
        if not isinstance(result, dict):
            return self._record_failure(
                claim, error="invalid_runner_result:%s" % type(result).__name__,
                retryable=False)

        reason = str(result.get("reason") or "")
        retryable = bool(result.get("retryable")) or reason == "provider_unavailable"
        return self._record_failure(
            claim, error=str(result.get("error") or reason or "run_failed"),
            retryable=retryable)

    def run_pending(self, max_events: Optional[int] = None) -> Dict[str, int]:
        """连续处理到没有可领取事件（或达到 *max_events*）。"""
        summary = {"processed": 0, "done": 0, "retried": 0, "failed": 0}
        while max_events is None or summary["processed"] < max_events:
            outcome = self.process_one()
            if not outcome.get("processed"):
                break
            summary["processed"] += 1
            state = outcome.get("state")
            if state == "done":
                summary["done"] += 1
            elif state == "failed":
                summary["failed"] += 1
            else:
                summary["retried"] += 1
        return summary


__all__ = [
    "MemoryEventWorker",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_BACKOFF_BASE_SECONDS",
    "DEFAULT_BACKOFF_MAX_SECONDS",
]
