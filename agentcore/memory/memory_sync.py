#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆库增量同步：把 A 库有、B 库没有的事实挑出来导出，再在 B 侧导入。

为什么不用直接拷库（2026-10-23）：
    直接 scp 整个 .db 会覆盖对方独有数据。实测远端宿主上有 4 条本机没有的事实
    （auto_trigger 1 + internet_ingest 3），拷过去就没了。

做法：
    1. 按 (statement 归一化哈希, workspace_id) 判重 —— 不用 fact_id，
       因为两边各自独立入库，同一句话的 fact_id 不同；
    2. 导出 JSONL（含 tags），对方 import 即可；
    3. tags 一并带过去，否则分桶信息丢失，召回会串池。

.. warning::
   **STATUS: 未接线（NOT WIRED）。**
   本模块在仓内没有任何调用方（代码与测试均零引用），保留为已设计但尚未接入
   主链路的参考实现。不要按「它已在工作」来理解记忆引擎的行为；
   接线前请先补调用方与对应测试。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple


def _norm(text: str) -> str:
    return hashlib.sha1(re.sub(r"\s+", "", text or "").encode("utf-8")).hexdigest()


def _db_path(explicit: str = "") -> Path:
    if explicit:
        return Path(explicit)
    import os
    return Path(os.path.expanduser("~/.hy_memory/data/coding_memory_cv.db"))


def _load_keys(conn: sqlite3.Connection) -> Set[Tuple[str, str]]:
    rows = conn.execute(
        "SELECT statement, workspace_id FROM working_fact WHERE status='active'").fetchall()
    return {(_norm(r[0]), r[1] or "") for r in rows}


def export_missing(src_db: str, dst_db: str, out_path: str, limit: int = 0) -> Dict[str, Any]:
    """导出 src 有、dst 没有的事实。"""
    src = sqlite3.connect(str(_db_path(src_db)))
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(str(_db_path(dst_db)))
    rows: List[Any] = []
    try:
        existing = _load_keys(dst)
        rows = src.execute(
            "SELECT * FROM working_fact WHERE status='active'").fetchall()
        exported: List[Dict[str, Any]] = []
        for r in rows:
            key = (_norm(r["statement"]), r["workspace_id"] or "")
            if key in existing:
                continue
            tags = {t["key"]: t["value"] for t in src.execute(
                "SELECT key, value FROM fact_tags WHERE fact_id=?",
                (r["fact_id"],)).fetchall()}
            exported.append({
                "statement": r["statement"],
                "subject": r["subject"] or "",
                "details": r["details"] or "",
                "ebm": "" if r["workspace_id"] == "_unknown" else (r["workspace_id"] or ""),
                "source": r["source"] or "",
                "user_id": r["user_id"] or get_config().default_user,
                "tags": tags,
            })
            if limit and len(exported) >= limit:
                break
    finally:
        src.close()
        dst.close()

    out = Path(out_path)
    with out.open("w", encoding="utf-8") as f:
        for item in exported:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    return {"ok": True, "scanned": len(rows), "exported": len(exported), "file": str(out)}


def import_pack(pack_path: str) -> Dict[str, Any]:
    """把导出的 JSONL 写入目标库。走 write_fact，幂等判重天然生效。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        from . import memory_store
    except ImportError as exc:
        return {"ok": False, "error": str(exc)}

    written = skipped = failed = 0
    for line in Path(pack_path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            failed += 1
            continue
        try:
            res = memory_store.write_fact(
                item["statement"],
                ebm=item.get("ebm") or None,
                subject=item.get("subject") or "",
                details=item.get("details") or "",
                tags=item.get("tags") or {},
                source=item.get("source") or "sync",
                user_id=item.get("user_id") or get_config().default_user,
                resolve_conflict=False)
            if res.get("written"):
                written += 1
            else:
                skipped += 1
        except Exception as exc:  # noqa: BLE001 —— 单条失败不中断整批
            print("写入失败: %s" % exc, file=sys.stderr)
            failed += 1
    return {"ok": True, "written": written, "skipped": skipped, "failed": failed}


def main() -> int:
    ap = argparse.ArgumentParser(description="记忆库增量同步")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ep = sub.add_parser("export", help="导出对方缺失的事实")
    ep.add_argument("--src", default="", help="源库（默认本机库）")
    ep.add_argument("--dst", default="", help="目标库（用于比对）")
    ep.add_argument("--out", required=True)
    ep.add_argument("--limit", type=int, default=0)

    ip = sub.add_parser("import", help="导入导出的包")
    ip.add_argument("--pack", required=True)

    args = ap.parse_args()
    res = (export_missing(args.src, args.dst, args.out, args.limit)
           if args.cmd == "export" else import_pack(args.pack))
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
