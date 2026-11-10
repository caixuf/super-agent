#!/usr/bin/env python3
"""Combine entity rules and profile-scoped memory without hiding backend failures."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def run(argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=12, check=False)
    if result.returncode != 0 or len(result.stdout) > 1048576:
        raise RuntimeError("recall_backend_failed")
    data = json.loads(result.stdout)
    if not isinstance(data, dict) or data.get("ok") is not True or not isinstance(data.get("results"), list):
        raise RuntimeError("recall_backend_failed")
    if any(not isinstance(row, dict) or not isinstance(row.get("statement"), str) for row in data["results"]):
        raise RuntimeError("recall_backend_failed")
    return data["results"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--entity-db", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--embed-url", default="", help="optional embedding endpoint; omit for keyword-only memory recall")
    parser.add_argument("--fusion", choices=("interleave", "rrf"), default="interleave")
    parser.add_argument("--in-process", action="store_true", help="Run both stores in-process without subprocesses")
    parser.add_argument("--include-reference", action="store_true", help="Include reference scope book/doc chunks")
    parser.add_argument("--diagnostic-file", default="", help="Optional atomic JSON diagnostic output path")
    args = parser.parse_args()
    if not 1 <= args.limit <= 20:
        parser.error("invalid limit")

    from agentcore.memory.hybrid_recall import recall_hybrid, rrf_fuse

    if args.in_process:
        import os
        from agentcore.memory.config import MemoryConfig, configure
        root = Path(args.data_dir)
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise ValueError("private memory directory required")
        info = root.stat()
        if info.st_mode & 0o077 or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
            raise ValueError("private memory directory required")
        if not (root / "coding_memory_cv.db").is_file():
            raise ValueError("memory database unavailable")
        embed_fn = lambda texts: None
        if args.embed_url:
            from agentcore.memory.bge_sidecar import make_embedder
            embed_fn = make_embedder(args.embed_url, dimension=1024)
        configure(MemoryConfig(data_dir=root, default_user=args.user, default_agent=args.agent, embed_fn=embed_fn))
        hybrid = recall_hybrid(
            args.query,
            limit=args.limit,
            entity_db=args.entity_db,
            user_id=args.user,
            agent_id=args.agent,
            profile=args.profile,
            exclude_reference=not args.include_reference,
            include_reference=args.include_reference,
            fusion=args.fusion,
        )
        rows = []
        for r in hybrid.get("results", []):
            entry = {"statement": r["statement"]}
            if r.get("fact_id"):
                entry["fact_id"] = r["fact_id"]
            for extra_key in ("polarity", "freshness_warning", "stale_file", "coactivated_from", "source_store"):
                if extra_key in r and r[extra_key] not in (None, ""):
                    entry[extra_key] = r[extra_key]
            rows.append(entry)
    else:
        entity = run([sys.executable, str(ROOT / "recall_entity.py"), "--query=" + args.query,
                      "--limit", str(args.limit), "--db", args.entity_db])
        mem_cmd = [sys.executable, str(ROOT / "recall_memory.py"), "--query-text=" + args.query,
                   "--limit", str(args.limit), "--data-dir", args.data_dir, "--user", args.user,
                   "--agent", args.agent, "--profile", args.profile, "--embed-url", args.embed_url]
        if args.include_reference:
            mem_cmd.append("--include-reference")
        memory = run(mem_cmd)
        rows = rrf_fuse(
            entity,
            memory,
            query=args.query,
            limit=args.limit,
            interleave=(args.fusion == "interleave"),
        )
    if args.diagnostic_file:
        try:
            import os
            import time
            diag_path = Path(args.diagnostic_file)
            if diag_path.is_absolute():
                diag_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = diag_path.with_suffix(f".tmp.{os.getpid()}")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump({"at": time.time(), "code": "hit" if rows else "no_match", "count": len(rows)}, stream)
                os.replace(tmp, diag_path)
        except Exception:
            pass
    payload = {"ok": True, "results": rows}
    if not rows:
        payload["empty_reason"] = "no_match"
    print(json.dumps(payload, ensure_ascii=False))

if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(json.dumps({"ok": False, "error": "memory_recall_unavailable", "results": []}))
        sys.exit(1)
