#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量灌入：把文档切分后写进记忆库，并建立向量索引。

为什么需要「批量」而不是一条条 write_fact：
    批量写入要处理三件事 —— 切分、去重、向量化。逐条调用会把向量化变成
    成百上千次单独请求，几秒变几十分钟；而且没有统一的进度与失败汇总。

切分原则（决定记忆质量的成败）：
    1. 按标题层级切，不是按字数切。一段话讲一半最伤检索。
    2. 每块带上前置标题路径（「7.2 通用记忆 > 调用点」），脱开文档也看得懂。
    3. 过短的块（<80 字）合并到上一块；过长（>1500 字）再按段落二次切。
    4. 保留来源（文件 + 标题），将来能回溯到原文。

安全约束：
    - 默认 dry-run，先看切出多少块、有没有明显垃圾，再决定是否落库。
    - 落库走 write_fact 的幂等判重，重复灌同一份文档不会翻倍。
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .config import get_config, logger_name

logger = logging.getLogger(logger_name("ingest"))

MIN_CHUNK_CHARS = 50

#: 仓库元数据文件，不是知识正文 —— 灌进来只会稀释检索
SKIP_NAMES = {"AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "TRANSLATION_NOTES.md",
              "TRANSLATION_STATUS.md", "CONTRIBUTORS.md", "LINKS.md", "LICENSE.md",
              "CODE_OF_CONDUCT.md", "SECURITY.md", "CHANGELOG.md", "README.md"}
MAX_ATOMIC_CHARS = 150   # 原子事实上限：超过就是段落，不是事实
MAX_CHUNK_CHARS = 1500


def _split_long(text: str, limit: int = MAX_CHUNK_CHARS) -> List[str]:
    """超长段落按句子边界二次切，尽量不切断句子。"""
    if len(text) <= limit:
        return [text]
    parts: List[str] = []
    buf = ""
    for para in re.split(r"(?<=[。；\n])", text):
        if not para.strip():
            continue
        if len(buf) + len(para) > limit and buf:
            parts.append(buf.strip())
            buf = para
        else:
            buf += para
    if buf.strip():
        parts.append(buf.strip())
    return parts or [text[:limit]]


def split_markdown(text: str, source: str = "") -> List[Dict[str, Any]]:
    """按标题层级切分 Markdown，返回 [{content, title_path, source}]。

    source 会作为根标题：技能库这类文件首个标题前往往是一段「技能定位」，
    没有它这块就丢了归属，检索时看不出属于哪个技能。
    """
    chunks: List[Dict[str, Any]] = []
    heading_stack: List[str] = [source] if source else []
    buf_lines: List[str] = []

    def flush() -> None:
        nonlocal buf_lines
        body = "\n".join(buf_lines).strip()
        buf_lines = []
        if not body:
            return
        title = " > ".join(heading_stack) if heading_stack else source
        pieces = _split_long(body)
        for i, piece in enumerate(pieces):
            suffix = "" if len(pieces) == 1 else " (%d/%d)" % (i + 1, len(pieces))
            chunks.append({"content": piece, "title_path": title + suffix,
                           "source": source})

    for line in (text or "").splitlines():
        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if m:
            flush()
            level = len(m.group(1))
            heading_stack = heading_stack[:level - 1] + [m.group(2).strip()]
            continue
        buf_lines.append(line)
    flush()

    merged: List[Dict[str, Any]] = []
    for c in chunks:
        if merged and len(c["content"]) < MIN_CHUNK_CHARS:
            merged[-1]["content"] += "\n" + c["content"]
        else:
            merged.append(c)
    return merged


def _looks_like_code(text: str) -> bool:
    """判断一段文本是不是代码，而不是自然语言知识。

    实测教训：灌 apachecn 那批书时 13%（15462 条）是代码块，其中单个
    solution_code.md 贡献了 9062 条。代码对**语义检索**帮助很小 ——
    问「怎么调缓冲区」时，一段 C 代码的向量和问题的语义距离很远，
    但它会占满候选位，把真正讲原理的段落挤出去。

    判据用「符号密度」而不是简单关键词：代码的特征是花括号/分号/缩进密集。
    """
    s = (text or "").strip()
    if len(s) < 20:
        return False
    code_chars = sum(s.count(c) for c in "{};=()<>[]")
    ratio = code_chars / max(len(s), 1)
    # 中文自然语言里这些符号占比通常 <5%；代码普遍 >10%
    if ratio > 0.10:
        return True
    # 明确的代码特征行
    for marker in ("def ", "function ", "#include", "class ", "import ", "public ", "void "):
        if marker in s[:80]:
            return True
    return False


def to_atomic_facts(text: str, limit: int = MAX_ATOMIC_CHARS,
                    drop_code: bool = True) -> List[str]:
    """把段落切成 50~150 字的原子事实。

    为什么必须切（架构红线）：整篇文章直接入库会让向量严重散焦 —— 一条 2000 字
    的块，向量是全部主题的平均，问任何一个具体问题时相似度都不高，检出来也只能
    看到一堆与问题无关的上下文。

    切法：按句号/分号/换行断句，累积到接近 limit 就断开；单句超长则硬切。
    过短的碎片（<20 字）并入上一句 —— 孤立的「注意：」没有任何检索价值。
    """
    body = (text or "").strip()
    if not body:
        return []
    sentences = [s.strip() for s in re.split(r"(?<=[。！？；\n])", body) if s.strip()]
    out: List[str] = []
    buf = ""
    for s in sentences:
        if len(s) > limit:
            if buf:
                out.append(buf)
                buf = ""
            for i in range(0, len(s), limit):
                out.append(s[i:i + limit])
            continue
        if len(buf) + len(s) > limit and buf:
            out.append(buf)
            buf = s
        else:
            buf += s
    if buf:
        out.append(buf)

    merged: List[str] = []
    for piece in out:
        piece = piece.strip()
        if not piece:
            continue
        if merged and len(piece) < 20:
            merged[-1] += piece
        else:
            merged.append(piece)
    kept = [m for m in merged if len(m) >= 20]
    if drop_code:
        # 代码块对语义检索价值低，且会挤占候选位（实测占灌入量的 13%）
        kept = [m for m in kept if not _looks_like_code(m)]
    return kept


def _dedup_key(chunk: Dict[str, Any]) -> str:
    norm = re.sub(r"\s+", "", chunk.get("content") or "")
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def ingest_paths(paths: Iterable[str], *, kind: str = "technical",
                 ebm: str = "", dry_run: bool = True, max_chunks: int = 0,
                 source: str = "") -> Dict[str, Any]:
    """把文件/目录切块并写入记忆库。dry_run 时只统计不落库。"""
    files: List[Path] = []
    # 相对路径的基准必须是**书的根目录**，不是调用方传进来的那个子目录：
    # 实测踩过 —— 传 docs/master-linux-sec-hard 时相对路径算出来是 06.md，
    # 领域照样推断不出。取所有目录参数的公共父级。
    _dirs = [Path(p) for p in paths if Path(p).is_dir()]
    roots = []
    if _dirs:
        try:
            common = Path(os.path.commonpath([str(d) for d in _dirs]))
            roots = [common.parent if common.name and common.is_dir() else common]
        except ValueError:  # 不同盘符/无关路径
            roots = _dirs
    for p in paths:
        path = Path(p)
        if path.is_dir():
            files.extend(sorted(f for f in path.rglob("*.md") if f.name not in SKIP_NAMES))
        elif path.is_file():
            files.append(path)
    if not files:
        return {"ok": False, "error": "没有找到可导入的 .md 文件", "files": 0}

    chunks: List[Dict[str, Any]] = []
    seen = set()
    per_file: Dict[str, int] = {}
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("读取失败 %s: %s", f, exc)
            continue
        # 整份文件级的代码过滤：按块判会漏 —— 代码被切成 100 字碎片后，
        # 每块的符号密度降下来了，但整份文件仍然是代码（实测 solution_code.md
        # 1.9MB 切成 2656 块，逐块判几乎全放过）。
        # 用「代码行占比」而不是「符号密度」：
        # 代码和注释混排时整体符号密度会被稀释（实测 solution_code.md 只有
        # 6.8%，低于阈值而被放过），但**逐行看代码行占绝大多数**。
        _lines = [ln for ln in text.splitlines() if ln.strip()]
        _code_lines = 0
        for ln in _lines:
            s = ln.strip()
            if (s.endswith((";", "{", "}", ":")) or s.startswith(("def ", "class ",
                    "import ", "from ", "#include", "//", "/*", "*", "public ",
                    "private ", "void ", "int ", "return "))
                    or sum(s.count(c) for c in "{};=()") / max(len(s), 1) > 0.12):
                _code_lines += 1
        file_code_ratio = _code_lines / max(len(_lines), 1)
        # 阈值定 0.85 而不是 0.5：
        # 0.5 会误杀「动态规划之KMP字符匹配算法」这类**文章 + 附代码**的文件 ——
        # 文章本身是有价值的自然语言知识，只是附带代码块。
        # 只有几乎全是代码（solution_code.md 这类纯代码库）才该整份跳过。
        if file_code_ratio > 0.75:
            logger.info("跳过纯代码文件 %s（代码行占 %.0f%%）", f.name, file_code_ratio * 100)
            per_file[f.name] = 0
            continue

        got = 0
        # subject 里必须是**相对路径**而不只是文件名：
        # apachecn 那批书每篇都叫 05.md / 07.md，只用文件名完全推断不出领域
        # （实测 87% 落进 general）。带上前缀目录才能命中 SOURCE_PREFIXES。
        rel_name = f.name
        for base in roots:
            try:
                rel_name = str(f.relative_to(base)).replace("\\", "/")
                break
            except ValueError as exc:
                # 不是异常，是「这个 base 不是它的祖先」的正常试探，换下一个
                logger.debug("相对路径基准不匹配 %s: %s", base, exc)
                continue
        # 每个标题块再拆成原子事实：整块入库会让向量散焦（见 to_atomic_facts 文档）
        for c in split_markdown(text, source=rel_name):
            atoms = to_atomic_facts(c["content"]) or [c["content"]]
            for atom in atoms:
                item = {"content": atom, "title_path": c["title_path"],
                        "source": rel_name}
                key = _dedup_key(item)
                if key in seen:
                    continue
                seen.add(key)
                chunks.append(item)
                got += 1
        per_file[f.name] = got

    if max_chunks and len(chunks) > max_chunks:
        chunks = chunks[:max_chunks]

    summary: Dict[str, Any] = {"ok": True, "files": len(files), "chunks": len(chunks),
                               "per_file": per_file, "dry_run": dry_run, "kind": kind}
    if dry_run:
        summary["sample"] = [{"title": c["title_path"][:80],
                              "chars": len(c["content"])} for c in chunks[:5]]
        return summary

    # 批量灌入是「可重跑」的批处理：每条记录付一次 fsync 不值得。
    # 实测远端宿主上默认 FULL 只有 1.4~8.7 条/秒（fsync 73ms/次），
    # 一次性灌 7 本书要跑一整天。设成 NORMAL 后 474 条/秒。
    # 必须在 import memory_store **之前**设好 —— 开关在建连接时读取。
    os.environ.setdefault("MEMORY_BULK_INGEST", "1")
    try:
        from . import memory_store
    except ImportError:
        import sys as _sys
        from pathlib import Path as _Path
        _root = str(_Path(__file__).resolve().parent.parent)
        if _root not in _sys.path:
            _sys.path.insert(0, _root)
        from . import memory_store  # type: ignore

    written = 0
    skipped = 0
    # 来源标记是回滚与追溯的唯一依据。批量语料质量不佳时，一条 SQL 即可全部作废：
    #   UPDATE working_fact SET status=deprecated WHERE source=<source>
    src_label = source or ("doc_ingest" if not ebm else "platform_ingest")
    for c in chunks:
        try:
            res = memory_store.write_fact(
                c["content"], ebm=ebm or None, subject=c["title_path"][:60],
                tags={"kind": kind, "origin": c.get("source", "doc")},
                source=src_label)
            if res.get("written"):
                written += 1
            else:
                skipped += 1
        except Exception as exc:  # noqa: BLE001 —— 单块失败不中断整批
            logger.warning("写入失败（%s）: %s", c["title_path"][:40], exc)

    # 批量完成后，统一增量构建向量索引（避免逐条调模型的等待）
    if written > 0:
        try:
            idx_res = memory_store.reindex_embeddings()
            summary["reindexed"] = idx_res.get("updated", 0)
            if not idx_res.get("ok"):
                # Facts and some vectors may already be committed; expose the
                # partial result so callers do not mistake it for a full import.
                summary["ok"] = False
                summary["error"] = "vector_reindex_incomplete"
                summary["reindex_result"] = idx_res
        except Exception as exc:  # noqa: BLE001
            logger.warning("批量向量化触发失败: %s", exc)
            summary["ok"] = False
            summary["error"] = "vector_reindex_failed"

    summary.update({"written": written, "skipped": skipped})
    return summary



def main() -> int:
    ap = argparse.ArgumentParser(description="批量灌入文档到记忆库")
    ap.add_argument("paths", nargs="+", help="文件或目录（.md）")
    ap.add_argument("--kind", default="technical",
                    choices=["technical", "personal", "context"])
    ap.add_argument("--ebm", default="", help="代际标识（宿主自定义）；不填进 unknown 桶")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只试运行）")
    ap.add_argument("--max-chunks", type=int, default=0, help="最多导入多少块（0=不限）")
    ap.add_argument("--source", default="", help="来源标记，用于追溯与批量回滚")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    res = ingest_paths(args.paths, kind=args.kind, ebm=args.ebm,
                       dry_run=not args.apply, max_chunks=args.max_chunks,
                       source=args.source)
    if not res.get("ok"):
        print("[ERR]", res.get("error"))
        if res.get("written"):
            print("部分结果已提交：写入事实 %d 条，更新向量 %d 条；请检查后再重试。" %
                  (res["written"], res.get("reindexed", 0)))
        return 1
    print("文件数: %d  切块数: %d  模式: %s" % (
        res["files"], res["chunks"], "试运行" if res["dry_run"] else "写入"))
    for name, n in sorted(res["per_file"].items(), key=lambda x: -x[1])[:10]:
        print("  %5d  %s" % (n, name))
    if res["dry_run"]:
        print("--- 抽样 ---")
        for s in res.get("sample") or []:
            print("  [%d字] %s" % (s["chars"], s["title"]))
        print("确认无误后加 --apply 执行写入")
    else:
        print("写入 %d 条，跳过重复 %d 条" % (res["written"], res["skipped"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
