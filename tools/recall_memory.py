#!/usr/bin/env python3
"""Read a single profile/user/agent memory store as localvec-compatible JSON."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agentcore.memory.config import MemoryConfig, configure
from agentcore.memory.memory_scope import CORE_SCOPES
from agentcore.memory.memory_store import recall_facts


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("query", nargs="?")
    p.add_argument("--query-text", default=None)
    p.add_argument("--data-dir", type=Path, required=True)
    p.add_argument("--user", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--profile", required=True)
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--embed-dim", type=int, default=1024)
    p.add_argument("--embed-url", default=os.environ.get("SUPERAGENT_MEMORY_EMBED_URL", ""),
                   help="optional embedding endpoint; omit for keyword-only recall")
    p.add_argument("--include-reference", action="store_true",
                   help="include external reference book/doc chunks in recall")
    p.add_argument("--scopes", nargs="*", default=None,
                   help="explicit scopes filter; defaults to core scopes (platform/principle/ops)")
    args = p.parse_args()
    if args.query_text is not None and args.query is not None:
        p.error("query must be supplied only once")
    query = args.query_text if args.query_text is not None else args.query
    if query is None:
        p.error("query is required")
    if not 1 <= args.limit <= 20 or not 1 <= args.embed_dim <= 8192:
        p.error("invalid limit or embedding dimension")
    if any(not x.strip() or len(x) > 512 for x in (args.user, args.agent, args.profile)):
        p.error("invalid principal")
    root = args.data_dir
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError("private memory directory required")
    info = root.stat()
    if info.st_mode & 0o077 or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
        raise ValueError("private memory directory required")
    if not (root / "coding_memory_cv.db").is_file():
        raise ValueError("memory database unavailable")
    # Without an explicitly configured endpoint, stay local and deterministic.
    # MemoryConfig's None otherwise enables a potentially slow implicit Ollama
    # lookup, which can exceed the caller's subprocess deadline before fallback.
    embed_fn = lambda texts: None
    if args.embed_url:
        from agentcore.memory.bge_sidecar import make_embedder
        embed_fn = make_embedder(args.embed_url, dimension=args.embed_dim)
    configure(MemoryConfig(data_dir=root, default_user=args.user, default_agent=args.agent,
                           embed_dim=args.embed_dim, embed_fn=embed_fn))
    scopes = args.scopes if args.scopes else (None if args.include_reference else list(CORE_SCOPES))
    result = recall_facts(query, user_id=args.user, agent_id=args.agent,
                          profile=args.profile, limit=args.limit, scopes=scopes)
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("memory recall failed")
    rows = result.get("results")
    if not isinstance(rows, list):
        raise RuntimeError("invalid memory recall result")
    # Return only data the localvec provider needs; do not expose internal paths,
    # source bodies, tags or confidence metadata.
    clean = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("statement"), str):
            continue
        item = {"fact_id": row.get("fact_id", ""), "statement": row.get("statement", "")}
        if row.get("freshness_warning"):
            item["freshness_warning"] = str(row["freshness_warning"])
        if row.get("bookmark_pinned"):
            item["bookmark_pinned"] = True
        tags = row.get("tags") or {}
        if isinstance(tags, dict) and tags.get("polarity") in ("prohibition", "requirement"):
            item["polarity"] = tags["polarity"]
        clean.append(item)
    payload = {"ok": True, "results": clean}
    if not clean:
        payload["empty_reason"] = result.get("empty_reason", "no_match")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Do not expose personal text, paths or provider exceptions in stderr.
        print(json.dumps({"ok": False, "error": "memory_recall_unavailable", "results": []}))
        sys.exit(1)
