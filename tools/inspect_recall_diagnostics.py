#!/usr/bin/env python3
"""Inspect bounded recall diagnostics without exposing queries or fact contents."""
from __future__ import annotations
import argparse
import json
import math
import os
from pathlib import Path
import re
import stat
import time

_NAME = re.compile(r"[a-zA-Z0-9_-]{1,64}\Z")
_SUCCESS = {"hit", "no_match"}
_FAILURE = {"backend_import_failed", "backend_query_failed"}


def inspect(path: Path, *, now: float, max_age: float) -> dict:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            st = os.fstat(stream.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or (hasattr(os, "getuid") and st.st_uid != os.getuid()):
                return {"status": "unknown", "reason": "unsafe_file"}
            raw = stream.read(16385)
        if len(raw) > 16384:
            return {"status": "unknown", "reason": "oversize"}
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise ValueError("not an object")
        stamp, code, count = obj.get("at"), obj.get("code"), obj.get("count")
        if (type(stamp) not in (int, float) or not math.isfinite(stamp)
                or stamp < 0 or stamp > now + 60 or code not in _SUCCESS | _FAILURE
                or type(count) is not int or count < 0):
            raise ValueError("invalid diagnostic")
        age = max(0, now - stamp)
        if age > max_age:
            return {"status": "unknown", "reason": "stale", "age_seconds": round(age)}
        return {"status": "healthy" if code in _SUCCESS else "failed",
                "code": code, "age_seconds": round(age), "count": count}
    except FileNotFoundError:
        return {"status": "unknown", "reason": "not_observed"}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"status": "unknown", "reason": "unreadable_or_invalid"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", required=True, metavar="NAME=ABSOLUTE_PATH")
    parser.add_argument("--max-age", type=float, default=3600)
    args = parser.parse_args(argv)
    if not math.isfinite(args.max_age) or not 1 <= args.max_age <= 604800 or len(args.source) > 64:
        parser.error("invalid age or source count")
    sources = {}
    for item in args.source:
        name, sep, raw = item.partition("=")
        if not sep or not _NAME.fullmatch(name) or name in sources or not Path(raw).is_absolute():
            parser.error("each source requires a unique name and an absolute path")
        sources[name] = Path(raw)
    now = time.time()
    result = {name: inspect(path, now=now, max_age=args.max_age) for name, path in sources.items()}
    print(json.dumps({"sources": result}, ensure_ascii=False, sort_keys=True))
    statuses = {item["status"] for item in result.values()}
    return 1 if "failed" in statuses else 2 if "unknown" in statuses else 0


if __name__ == "__main__":
    raise SystemExit(main())
