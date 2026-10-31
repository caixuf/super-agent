#!/usr/bin/env python3
"""tools/recall_entity.py — localvec 召回子进程执行器。

契约遵循 plugins/localvec:
输入：命令行参数 {query} [--limit N] [--db PATH]
输出：stdout 输出标准 JSON：{"ok": true, "results": [...]}
性能要求：耗时 < 30ms，0 外部依赖，0 向量模型推理开销。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# 确保本仓根目录在 sys.path 中，使得 agentcore 可以直接导入
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentcore.memory.entity_store import EntityStore


def main() -> int:
    parser = argparse.ArgumentParser(description="Recall relevant entity rules for prompt prefetch.")
    parser.add_argument("query", nargs="?", default="", help="User prompt query")
    parser.add_argument("--query", dest="named_query", default="", help="Query alias")
    parser.add_argument("--limit", type=int, default=5, help="Max number of items to recall")
    parser.add_argument("--db", default=None, help="Path to SQLite entity rules database")

    args = parser.parse_args()
    q = (args.query or args.named_query or "").strip()
    limit = max(1, min(args.limit, 20))

    if not q:
        json.dump({"ok": True, "results": [], "empty_reason": "empty_query"}, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0

    t0 = time.monotonic()
    try:
        store = EntityStore(db_path=args.db)
        results = store.recall(q, limit=limit)
        elapsed_ms = (time.monotonic() - t0) * 1000.0

        payload = {
            "ok": True,
            "results": results,
            "latency_ms": round(elapsed_ms, 2),
            "count": len(results),
        }
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    except Exception as exc:
        json.dump({
            "ok": False,
            "error": str(exc),
            "results": [],
        }, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0


if __name__ == "__main__":
    sys.exit(main())
