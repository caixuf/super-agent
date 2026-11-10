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
from . import progressive
from . import monthly_log
from .hybrid_recall import recall_hybrid, rrf_fuse
from .memory_consolidate import consolidate_memories
from .memory_dream import MemoryDreamEngine
from .memory_graph import (
    add_edge,
    add_edges_batch,
    get_neighbors,
    traverse_paths,
    find_invalidation_blast_radius,
)
from .memory_conflict import get_evidence_tier, has_semantic_polarity_conflict
from .memory_plasticity import apply_task_outcome
from .memory_surprise import compute_surprise, extract_causal_triplet, should_flash_write
from .memory_sync import sync_databases
from .memory_store import (
    check_fact_freshness,
    dispute_fact,
    drop_fact,
    flush_background,
    list_tags,
    normalize_ebm,
    prune_dormant_vectors,
    query_fact_lineage,
    recall_facts,
    get_facts_by_ids,
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
    # 写入 / 召回 / 混合融合 / 可塑性 / 同步 / 向量回收 / 四大进化
    "write_fact",
    "recall_facts",
    "recall_hybrid",
    "rrf_fuse",
    "apply_task_outcome",
    "compute_surprise",
    "extract_causal_triplet",
    "should_flash_write",
    "consolidate_memories",
    "MemoryDreamEngine",
    "add_edge",
    "add_edges_batch",
    "get_neighbors",
    "traverse_paths",
    "find_invalidation_blast_radius",
    "get_evidence_tier",
    "has_semantic_polarity_conflict",
    "dispute_fact",
    "query_fact_lineage",
    "check_fact_freshness",
    "sync_databases",
    "prune_dormant_vectors",
    "get_facts_by_ids",
    "progressive",
    "monthly_log",
    "flush_background",
    "drop_fact",
    "supersede",
    "list_tags",
    "normalize_ebm",
    "reindex_embeddings",
]