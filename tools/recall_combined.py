#!/usr/bin/env python3
"""Combine entity rules and profile-scoped memory without hiding backend failures."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent

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
    parser.add_argument("--embed-url", default="")
    args = parser.parse_args()
    if not 1 <= args.limit <= 20:
        parser.error("invalid limit")
    entity = run([sys.executable, str(ROOT / "recall_entity.py"), "--query=" + args.query,
                  "--limit", str(args.limit), "--db", args.entity_db])
    memory = run([sys.executable, str(ROOT / "recall_memory.py"), "--query-text=" + args.query,
                  "--limit", str(args.limit), "--data-dir", args.data_dir, "--user", args.user,
                  "--agent", args.agent, "--profile", args.profile, "--embed-url", args.embed_url])
    # Alternate sources to prevent either full result set from starving the other.
    rows, seen = [], set()
    for pair in zip(entity + [None] * len(memory), memory + [None] * len(entity)):
        for row in pair:
            if row is None:
                continue
            statement = row["statement"].strip()
            if statement and statement not in seen:
                rows.append(row); seen.add(statement)
                if len(rows) >= args.limit:
                    break
        if len(rows) >= args.limit:
            break
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
