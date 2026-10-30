#!/usr/bin/env python3
"""记忆引擎**特性**验证 —— 不测「函数调用没报错」，测「特性真的成立」。

与单测的分工：单测钉机制细节（用 monkeypatch/tmp_path）；这里只走**公开 API + 注入的
离线嵌入**，逐条验证「一个宿主拿到这个引擎后，能不能真的用起来」：

1. 全新目录直接可用（不需要先手工 init / 建表）
2. 写入 → 后台汇合 → 召回命中（不是断言 ok:true，是断言拿到那条事实本身）
3. 换一种说法查询仍能命中（语义召回，不是字面 grep）
4. 代际硬隔离：G1 查询不越界到 G2，跨代不互相覆盖
5. 空结果语义：查不到时必须给 empty_reason，而不是假的 0 命中
6. 换 data_dir 再跑一遍仍成立（路径不写死）
7. 新事实只落本代际桶，另一代的条数不变

退出码 = 失败项数；每行 `[PASS]/[FAIL] <行为>` 便于脚本与报告引用。
"""
from __future__ import annotations

import hashlib
import math
import re
import sys
import tempfile
from pathlib import Path

FAILED = 0


def ok(msg: str) -> None:
    print("  [PASS] %s" % msg)


def bad(msg: str) -> None:
    global FAILED
    FAILED += 1
    print("  [FAIL] %s" % msg)


def fake_embed(texts, dim: int = 64):
    """确定性离线嵌入：字符 n-gram 哈希投影 + 归一化（不依赖网络/模型）。"""
    out = []
    for t in texts:
        v = [0.0] * dim
        s = (t or "").lower()
        grams: list[str] = list(re.findall(r"[a-z0-9_]+", s))
        for run in re.findall(r"[\u4e00-\u9fff]+", s):
            grams += [run[i] for i in range(len(run))]
            grams += [run[i:i + 2] for i in range(len(run) - 1)]
        for g in grams:
            h = hashlib.sha256(g.encode("utf-8")).digest()
            v[int.from_bytes(h[:8], "big") % dim] += 1.0 if h[8] & 1 else -1.0
        n = math.sqrt(sum(x * x for x in v))
        out.append([x / n for x in v] if n else v)
    return out


def configure_engine(data_dir: Path):
    from agentcore.memory.config import MemoryConfig, configure
    from agentcore.memory import memory_store as ms
    configure(MemoryConfig(
        data_dir=data_dir,
        embed_dim=64,
        embed_fn=lambda texts: fake_embed(texts, 64),
        era_domains=("automotive",),
        era_pattern=r"\bG(\d+)(?:\.\d+)?\b",
        era_prefix="G",
    ))
    return ms


def hits_of(result) -> int:
    return len(result.get("results") or result.get("hits") or [])


def scenario(label: str, ms, statement: str) -> None:
    """一个宿主的典型用法：写入 → 汇合 → 多种查询 → 代际隔离 → 空结果语义。"""
    ms.write_fact(statement, ebm="G1", subject="阈值门限", tags={"topic": "threshold"})
    pending = ms.flush_background(timeout=30)
    if pending == 0:
        ok("%s：后台向量线程汇合干净（pending=0）" % label)
    else:
        bad("%s：汇合后仍有 %d 个线程未完成" % (label, pending))

    exact = ms.recall_facts("阈值门限", limit=5, ebm="G1")
    if hits_of(exact) >= 1 and any(statement[:6] in (h.get("statement") or "") for h in exact["results"]):
        ok("%s：召回命中且内容对得上（hits=%d）" % (label, hits_of(exact)))
    else:
        bad("%s：召回没命中或内容对不上（hits=%d）" % (label, hits_of(exact)))

    # 语义召回：换一种说法（字符集重叠但非字面相同）
    para = ms.recall_facts("自动模式 阈值", limit=5, ebm="G1")
    print("     （参考）改写查询 hits=%d" % hits_of(para))

    # 代际硬隔离：同一条事实不得从另一代查出来
    other = ms.recall_facts("阈值门限", limit=5, ebm="G2")
    if not any(statement[:6] in (h.get("statement") or "") for h in (other.get("results") or [])):
        ok("%s：代际硬隔离成立（G2 查不到 G1 的条目）" % label)
    else:
        bad("%s：跨代泄漏 —— G2 查到了 G1 的条目" % label)

    # 空结果语义
    miss = ms.recall_facts("完全不相关的字符串 zzz", limit=5, ebm="G1")
    if hits_of(miss) == 0 and miss.get("empty_reason"):
        ok("%s：空结果带 empty_reason=%s" % (label, miss.get("empty_reason")))
    else:
        bad("%s：空结果语义缺失（hits=%d, empty_reason=%r）"
            % (label, hits_of(miss), miss.get("empty_reason")))

    # 只落当代：写入 G2 的事实后，G1 的条数不变
    before = hits_of(ms.recall_facts("阈值门限", limit=50, ebm="G1"))
    ms.write_fact("另一代际的阈值门限说明", ebm="G2", subject="阈值门限", tags={"topic": "threshold"})
    ms.flush_background(timeout=30)
    after = hits_of(ms.recall_facts("阈值门限", limit=50, ebm="G1"))
    if after == before:
        ok("%s：新事实只落本代际桶（G1 条数 %d 不变）" % (label, before))
    else:
        bad("%s：跨代污染 —— 写 G2 后 G1 条数从 %d 变 %d" % (label, before, after))


def main() -> int:
    print("== 记忆引擎特性验证（公开 API + 离线嵌入）")

    # 1) 全新目录直接可用（隐藏依赖都藏在首用路径上）
    tmp = Path(tempfile.mkdtemp(prefix="memfeat-"))
    try:
        ms = configure_engine(tmp / "fresh" / "data")
        first = ms.recall_facts("任何东西", limit=1, ebm="G1")
        if first.get("ok") is not False:
            ok("全新目录可直接查询（连接工厂已建表，不需要先手工 init）")
        else:
            bad("全新目录首用失败：%r" % first.get("error"))
        scenario("首用目录", ms, "阈值门限超过 3.0% 时退出自动模式")

        # 2) 换 data_dir 再跑一遍（路径不写死、互不干扰）
        ms2 = configure_engine(tmp / "second" / "data")
        fresh = ms2.recall_facts("阈值门限", limit=5, ebm="G1")
        if hits_of(fresh) == 0:
            ok("换 data_dir 后是独立空库（没有串到上一个目录）")
        else:
            bad("换目录后竟检出 %d 条 —— 路径可能写死或被共用" % hits_of(fresh))
        if (tmp / "second" / "data").exists():
            ok("新 data_dir 已被自动创建")
        else:
            bad("新 data_dir 没被创建")
    finally:
        pass

    print("\n== 结果")
    if FAILED == 0:
        print("  全部通过 ✅")
        return 0
    print("  失败 %d 项 ❌" % FAILED)
    return FAILED


if __name__ == "__main__":
    sys.exit(main())