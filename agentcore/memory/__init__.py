"""agentcore.memory —— 记忆引擎实现。

对外导出的最小可用面：配置注入 + 确定性写入 + 双路召回 + 后台汇合。
其余子模块（分桶、冲突、门禁、可塑性、灌入等）可按需单独导入。
"""

from __future__ import annotations

from .config import (
    MemoryConfig,
    configure,
    get_config,
    logger_name,
    reset_config,
)
from .memory_store import (
    drop_fact,
    flush_background,
    list_tags,
    normalize_ebm,
    recall_facts,
    reindex_embeddings,
    supersede,
    write_fact,
)

__all__ = [
    # 配置
    "MemoryConfig",
    "configure",
    "get_config",
    "reset_config",
    "logger_name",
    # 写入 / 召回
    "write_fact",
    "recall_facts",
    "flush_background",
    "drop_fact",
    "supersede",
    "list_tags",
    "normalize_ebm",
    "reindex_embeddings",
]