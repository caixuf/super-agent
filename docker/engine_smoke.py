#!/usr/bin/env python3
"""干净室引擎自检：写入 → 汇合 → 召回（离线确定性嵌入，不依赖网络/模型）。

放在 docker/ 下是有意的：它**只用本仓公开 API + 注入的离线嵌入**，
既是容器里那条端到端证据，也是「宿主该怎么注入业务约定」的可运行范例。

退出码 0 = 端到端成立；非 0 = 失败。最后一行固定打印 SMOKE_OK / SMOKE_FAIL 供脚本判定。
"""
from __future__ import annotations

import hashlib
import math
import re
import sys
import tempfile
from pathlib import Path


def fake_embed(texts, dim: int = 64):
    """确定性离线嵌入：字符 n-gram 哈希投影 + 归一化。生产环境换成真模型。"""
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


def main() -> int:
    from agentcore.memory.config import MemoryConfig, configure

    # 全新目录（隐藏依赖主要藏在「首次使用」路径上）
    data_dir = Path(tempfile.mkdtemp(prefix="agentcore-smoke-")) / "data"
    configure(MemoryConfig(
        data_dir=data_dir,
        embed_dim=64,
        embed_fn=lambda texts: fake_embed(texts, 64),
        era_domains=("automotive",),
        # 代际「写法」由宿主约定；这里用中性的 G 系写法
        era_pattern=r"\bG(\d+)(?:\.\d+)?\b",
        era_prefix="G",
    ))

    from agentcore.memory import memory_store as ms

    ms.write_fact(
        "阈值门限超过 3.0% 时退出自动模式",
        ebm="G1",
        subject="阈值门限",
        tags={"topic": "threshold"},
    )
    pending = ms.flush_background(timeout=30)
    r = ms.recall_facts("阈值门限", limit=3, ebm="G1")
    hits = len(r.get("results") or r.get("hits") or [])
    print("pending=%d hits=%d empty_reason=%s data_dir=%s"
          % (pending, hits, r.get("empty_reason"), data_dir))

    if hits == 1 and pending == 0:
        print("SMOKE_OK")
        return 0
    print("SMOKE_FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())