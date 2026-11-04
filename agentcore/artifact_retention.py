"""产物生命周期管理与交付过滤器 (Artifact Retention & Delivery Filter)。

【设计原则】：
1. 产物生命周期回收:
   - 统一收敛至标准目录 (默认 ~/.hermes/artifacts 或 /data/superagent/artifacts)；
   - 设定 7 天 TTL (默认 7 * 86400 秒)，定期清理过期历史临时文件与大报告，保障磁盘永不打满。
2. 交付物过滤:
   - 贯彻架构决议：保留并支持 HTML 交互报告回传；
   - 彻底关闭 (Disable) 臃肿的自动截图与图片转码，节省网络带宽并防止卡片膨胀。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS: float = 7 * 86400.0  # 7 天


@dataclass
class CleanupSummary:
    scanned_count: int
    removed_count: int
    freed_bytes: int


@dataclass
class DeliveryFilterPolicy:
    allow_html: bool = True
    allow_screenshots: bool = False  # 用户明确要求：截图不需要，彻底关闭

    def should_deliver_file(self, filename: str) -> bool:
        ext = os.path.splitext(filename)[1].lower()
        if ext in (".png", ".jpg", ".jpeg", ".webp"):
            return self.allow_screenshots
        if ext in (".html", ".htm"):
            return self.allow_html
        return True


class ArtifactRetentionManager:
    """管理产物目录的滚动清理与生命周期。"""

    def __init__(self, root_dir: str | Path, ttl_seconds: float = DEFAULT_TTL_SECONDS):
        self.root_dir = Path(root_dir)
        self.ttl_seconds = ttl_seconds

    def ensure_directory(self) -> Path:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        return self.root_dir

    def prune_expired(self, now: Optional[float] = None) -> CleanupSummary:
        """滚动扫描并删除超过 TTL 的过期产物。"""
        if not self.root_dir.exists():
            return CleanupSummary(scanned_count=0, removed_count=0, freed_bytes=0)

        current_ts = now or time.time()
        cutoff = current_ts - self.ttl_seconds
        scanned = 0
        removed = 0
        freed = 0

        for item in self.root_dir.rglob("*"):
            if not item.is_file():
                continue
            scanned += 1
            try:
                stat = item.stat()
                if stat.st_mtime < cutoff:
                    size = stat.st_size
                    item.unlink()
                    removed += 1
                    freed += size
                    logger.info("已清理过期产物文件: %s (%d bytes)", item.name, size)
            except OSError as exc:
                logger.warning("清理文件 %s 失败: %s", item, exc)

        return CleanupSummary(scanned_count=scanned, removed_count=removed, freed_bytes=freed)
