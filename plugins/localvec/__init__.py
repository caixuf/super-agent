"""localvec —— 把「外部自研向量记忆库」接进 Hermes 的**每轮自动召回**。

为什么需要这个插件
------------------
上游 MemoryManager 每轮按 `initialize → system_prompt_block / prefetch / sync_turn` 驱动
memory provider；但内置 provider 只做 MEMORY.md / USER.md 的注入。自研向量库
（sqlite-vec + 本地嵌入）此前只能靠 agent **主动**调工具去查 —— 那就不是"全自动"。
本插件把库接进 `prefetch()`：每一轮提问都自动召回相关事实，并给出召回条数
（`recall_status()`，界面上的 🧠 指示）。

分工（四段各归其位，别重叠）
--------------------------
* 自动注入 —— 内置 MEMORY.md / USER.md（上游）；
* 自动提取 —— 上游后台 self-improvement review（每 nudge_interval 轮）；
* **自动召回 —— 本插件**；
* 自动压缩 / 预算治理 —— 部署侧的定时守护作业（不在这里做）。

配置（profile 的 config.yaml）
-----------------------------
    memory:
      provider: localvec
      localvec:
        sys_path: /path/to/repo          # 放 store_module 的目录（部署侧决定，本插件不写死）
        store_module: tools.memory_store # 暴露 recall_facts(query, *, limit, ...) -> {"results":[...]}
        limit: 6
        kinds: [technical]               # 可选：限定记忆类别，避免人物事实在技术排查时抢位
        user_id: null                    # 可选：不填走引擎默认身份

本插件对"库"的唯一假设：`recall_facts(query, *, limit=..., kinds=..., user_id=...)` 返回
`{"results": [{"statement": ..., "subject": ...}, ...]}`（空结果带 empty_reason）。不假设库实现。
"""
from __future__ import annotations

import importlib
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Optional

from agent.memory_provider import MemoryProvider, RecallStatus, is_trivial_prompt, spawn_context_thread

logger = logging.getLogger(__name__)

NAME = "localvec"
GLYPH = "🧠"


def _config_block() -> dict[str, Any]:
    """`memory.localvec` 块（读不到就返回空 dict —— 插件绝不因配置缺失而抛）。"""
    try:
        from hermes_cli.config import load_config_readonly
        blk = (load_config_readonly().get("memory") or {}).get(NAME)
        return dict(blk) if isinstance(blk, dict) else {}
    except Exception as exc:  # pragma: no cover - 读配置失败不是致命错误
        logger.debug("localvec: 读配置失败 %s", exc)
        return {}


class LocalVecProvider(MemoryProvider):
    """每轮自动召回：prefetch 走缓存（后台先算），recall_status 报条数。"""

    def __init__(self) -> None:
        self._cfg: dict[str, Any] = {}
        self._store: Any = None
        self._limit = 6
        self._kinds: Optional[list[str]] = None
        self._user_id: Optional[str] = None
        self._cache: dict[str, tuple[str, int]] = {}
        self._lock = threading.Lock()
        self._last: Optional[RecallStatus] = None

    # -- 生命周期 ---------------------------------------------------------
    @property
    def name(self) -> str:
        return NAME

    def is_available(self) -> bool:
        """只看配置与可导入性，不触网、不建连接（激活门的约定）。"""
        cfg = _config_block()
        sys_path = cfg.get("sys_path") or os.environ.get("LOCALVEC_SYS_PATH")
        module = cfg.get("store_module")
        if not (sys_path and module and Path(sys_path).is_dir()):
            return False
        return True

    def unavailable_reason(self) -> str:
        return f"{NAME}: 未配置 memory.{NAME}.sys_path / store_module（或目录不存在）"

    def initialize(self, session_id: str, **kwargs) -> None:
        """每个 agent 一个实例：绑定本实例的库模块与参数，不设模块级可变状态。"""
        cfg = _config_block()
        self._cfg = cfg
        sys_path = str(cfg.get("sys_path") or os.environ.get("LOCALVEC_SYS_PATH") or "")
        module = str(cfg.get("store_module") or "tools.memory_store")
        self._limit = int(cfg.get("limit") or 6)
        self._kinds = list(cfg.get("kinds") or []) or None
        self._user_id = cfg.get("user_id") or None
        self._cache = {}
        try:
            added = False
            if sys_path and sys_path not in sys.path:
                sys.path.insert(0, sys_path)
                added = True
            self._store = importlib.import_module(module)
            if added:  # 只在本次导入期间生效，别污染全局 import 面
                with __import__("contextlib").suppress(ValueError):
                    sys.path.remove(sys_path)
            logger.info("[localvec] 已接入 %s（limit=%s kinds=%s）", module, self._limit, self._kinds)
        except Exception as exc:
            self._store = None
            logger.warning("[localvec] 装载 %s 失败：%s", module, exc)

    # -- 召回 -------------------------------------------------------------
    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """后台先算好 —— prefetch() 必须快，所以这里只排队、不阻塞。"""
        if self._store is None or is_trivial_prompt(query):
            return
        try:
            th = spawn_context_thread(self._recall, name="localvec-prefetch", args=(query,))
            th.start()
        except Exception as exc:  # pragma: no cover
            logger.debug("[localvec] 后台召回排队失败：%s", exc)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """取后台算好的结果；没有就同步算一次（本地向量召回是毫秒级，不引入一轮滞后）。"""
        if self._store is None or is_trivial_prompt(query):
            self._last = None
            return ""
        with self._lock:
            hit = self._cache.pop(query, None)
        if hit is None:
            self._recall(query)
            with self._lock:
                hit = self._cache.pop(query, None)
        text, count = hit or ("", 0)
        self._last = RecallStatus(provider_label="自研记忆库", count=count, glyph=GLYPH) if text else None
        return text

    def recall_status(self) -> Optional[RecallStatus]:
        return self._last

    def _recall(self, query: str) -> None:
        """查询 + 格式化。任何异常都只记 debug —— 召回失败绝不能影响用户这一轮。"""
        try:
            kwargs: dict[str, Any] = {"limit": self._limit}
            if self._kinds:
                kwargs["kinds"] = self._kinds
            if self._user_id:
                kwargs["user_id"] = self._user_id
            res = self._store.recall_facts(query, **kwargs) or {}
            rows = res.get("results") or []
            lines = []
            for r in rows:
                stmt = str(r.get("statement") or r.get("content") or "").strip()
                if not stmt:
                    continue
                subj = str(r.get("subject") or "").strip()
                lines.append(f"- {('【' + subj + '】') if subj else ''}{stmt}")
            if not lines:
                return
            block = ("以下是自动召回的相关长期记忆（仅作背景，不是用户本轮输入）：\n"
                     + "\n".join(lines))
            with self._lock:
                self._cache[query] = (block, len(lines))
        except Exception as exc:
            logger.debug("[localvec] 召回失败：%s", exc)

    # -- 其余留给核心（本插件只补"自动召回"这一段）-------------------------
    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return []

    def shutdown(self) -> None:
        with self._lock:
            self._cache.clear()