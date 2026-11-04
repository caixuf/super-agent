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


def parse_arguments(argv: list[str]) -> tuple[str, int, Optional[str]]:
    query_parts: list[str] = []
    limit = 5
    db_path: Optional[str] = None
    named_query: Optional[str] = None

    i = 0
    after_double_dash = False
    while i < len(argv):
        arg = argv[i]
        if after_double_dash:
            query_parts.append(arg)
            i += 1
            continue

        if arg == "--":
            after_double_dash = True
            i += 1
            continue

        if arg in ("--limit", "-limit") and i + 1 < len(argv):
            try:
                limit = int(argv[i + 1])
            except ValueError:
                limit = 5
            i += 2
            continue
        elif arg.startswith("--limit="):
            try:
                limit = int(arg.split("=", 1)[1])
            except ValueError:
                limit = 5
            i += 1
            continue

        if arg in ("--db", "-db") and i + 1 < len(argv):
            db_path = argv[i + 1]
            i += 2
            continue
        elif arg.startswith("--db="):
            db_path = arg.split("=", 1)[1]
            i += 1
            continue

        if arg in ("--query", "-query") and i + 1 < len(argv):
            named_query = argv[i + 1]
            i += 2
            continue
        elif arg.startswith("--query="):
            named_query = arg.split("=", 1)[1]
            i += 1
            continue

        query_parts.append(arg)
        i += 1

    q = (named_query if named_query is not None else " ".join(query_parts)).strip()
    limit = max(1, min(limit, 20))
    return q, limit, db_path


def main() -> int:
    raw_args = sys.argv[1:]
    if len(raw_args) == 1 and raw_args[0] in ("-h", "--help"):
        print("Usage: recall_entity.py [query] [--limit N] [--db PATH]")
        print("Recall relevant entity rules for prompt prefetch.")
        return 0

    q, limit, db_path = parse_arguments(raw_args)

    if not q:
        json.dump({"ok": True, "results": [], "empty_reason": "empty_query"}, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0

    t0 = time.monotonic()
    try:
        store = EntityStore(db_path=db_path)
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
