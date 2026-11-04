#!/usr/bin/env python3
"""Run scoped progressive memory operations in an isolated subprocess."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agentcore.memory.config import MemoryConfig, configure
from agentcore.memory import progressive


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", type=Path, required=True)
    p.add_argument("--user", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--profile", required=True)
    p.add_argument("--embed-url", default="")
    args = p.parse_args()
    if any(not x.strip() or len(x) > 512 for x in (args.user, args.agent, args.profile)):
        raise ValueError("invalid principal")
    root = args.data_dir
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError("invalid memory directory")
    info = root.stat()
    if info.st_mode & 0o077 or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
        raise ValueError("invalid memory directory")
    if not (root / "coding_memory_cv.db").is_file():
        raise ValueError("memory database unavailable")
    raw = sys.stdin.buffer.read(32769)
    if len(raw) > 32768:
        raise ValueError("request too large")
    request = json.loads(raw)
    if not isinstance(request, dict) or set(request) != {"operation", "arguments"}:
        raise ValueError("invalid request")
    operation, inputs = request["operation"], request["arguments"]
    if not isinstance(inputs, dict):
        raise ValueError("invalid arguments")
    embed_fn = None
    if args.embed_url:
        from agentcore.memory.bge_sidecar import make_embedder
        embed_fn = make_embedder(args.embed_url)
    configure(MemoryConfig(data_dir=root, default_user=args.user, default_agent=args.agent, embed_fn=embed_fn))
    identity = {"user_id": args.user, "agent_id": args.agent, "profile": args.profile}
    if operation == "search" and set(inputs) <= {"query", "limit"}:
        query, limit = inputs.get("query"), inputs.get("limit", 5)
        if not isinstance(query, str) or len(query) > 8000 or type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("invalid search")
        result = progressive.search(query, limit=limit, **identity)
    elif operation == "timeline" and set(inputs) <= {"ids", "query", "window"}:
        ids, query, window = inputs.get("ids", []), inputs.get("query"), inputs.get("window", 5)
        if (not isinstance(ids, list) or len(ids) > 50 or
                any(not isinstance(x, str) or len(x) > 128 for x in ids) or
                (query is not None and (not isinstance(query, str) or len(query) > 8000)) or
                type(window) is not int or not 1 <= window <= 50):
            raise ValueError("invalid timeline")
        result = progressive.timeline(ids or None, query=query, window=window, **identity)
    elif operation == "get" and set(inputs) == {"ids"}:
        ids = inputs["ids"]
        if not isinstance(ids, list) or len(ids) > 20 or any(not isinstance(x, str) or len(x) > 128 for x in ids):
            raise ValueError("invalid ids")
        result = progressive.get_observations(ids, **identity)
    else:
        raise ValueError("invalid operation")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print(json.dumps({"ok": False, "error": "memory_operation_unavailable"}))
        sys.exit(1)
