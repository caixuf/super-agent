#!/usr/bin/env python3
"""memory_budget_report.py —— 记忆占用与待批队列的**确定性**体检，供定时守护作业的 monitor 门控。

为什么要分桶、不报精确值：monitor 的语义是"输出没变就不唤醒 agent"。精确字符数每次写入都会变，
那样定时作业会变成刷屏器；分桶后只有**跨桶**才算状态变化 —— 这才是"该管的时候才管"。

输出（每行一个不变量，末行固定为状态摘要）：
    memory  90-94% chars=18782/20000 over85=true
    user    45-49% chars=3953/8000
    pending 6 批（可重放 0：destructive 缺 matched_entry 会被拒）
    state   memory=90-94 user=45-49 pending=6 over85=true

用法：
    python3 memory_budget_report.py [--profile-dir DIR]   # 打印报告（monitor 每 tick 调用）
    python3 memory_budget_report.py --json               # 给脚本用

profile 目录**不写死**：本仓是通用仓，具体 profile 名是部署侧的事 —— 由 `HERMES_PROFILE_DIR`
或 `--profile-dir` 传入（部署方的 shim 自己知道它是谁）。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def _default_profile() -> Path:
    """通用兜底：HERMES_PROFILE_DIR > HERMES_HOME/profiles/<HERMES_PROFILE|default>。"""
    env = os.environ.get("HERMES_PROFILE_DIR")
    if env:
        return Path(env)
    home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    return home / "profiles" / (os.environ.get("HERMES_PROFILE") or "default")


def _limits(cfg_path: Path) -> tuple[int, int]:
    """配额的唯一事实源是 profile 的 config.yaml。"""
    try:
        import yaml  # type: ignore
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        cfg = {}
    mem = cfg.get("memory") or {}
    return int(mem.get("memory_char_limit", 20000)), int(mem.get("user_char_limit", 8000))


def _chars(path: Path) -> int:
    """按**字符**计数 —— 磁盘字节数会因 CJK（3 字节/字）虚高一倍，按字节判配额是错的。"""
    try:
        return len(path.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return 0


def _bucket(pct: int) -> str:
    lo = min(95, max(0, pct // 5 * 5))
    return f"{lo}-{lo + 4}"


def _pending(profile: Path) -> tuple[int, int]:
    """待批记忆批次数 + 其中"可重放"的条数。

    可重放的判据来自 tools/memory_tool.apply_memory_pending：破坏性操作必须带 matched_entry
    （条目固定），否则会被拒（"predates entry pinning"）。旧提案多半是这种，所以这个数很重要。
    """
    d = profile / "pending" / "memory"
    total = replayable = 0
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        total += 1
        ops = (payload.get("payload") or {}).get("operations") or []
        destructive = [o for o in ops if o.get("action") in ("patch", "remove", "replace")]
        if len(destructive) == len([o for o in destructive if o.get("matched_entry")]):
            replayable += 1
    return total, replayable


def report(profile: Path, as_json: bool = False) -> dict:
    mem_limit, user_limit = _limits(profile / "config.yaml")
    mem_chars, user_chars = _chars(profile / "memories" / "MEMORY.md"), _chars(profile / "memories" / "USER.md")
    mem_pct = round(100 * mem_chars / mem_limit) if mem_limit else 0
    user_pct = round(100 * user_chars / user_limit) if user_limit else 0
    pend_total, pend_replay = _pending(profile)
    out = {
        "memory": {"chars": mem_chars, "limit": mem_limit, "pct": mem_pct,
                   "bucket": _bucket(mem_pct), "over85": mem_pct >= 85},
        "user": {"chars": user_chars, "limit": user_limit, "pct": user_pct, "bucket": _bucket(user_pct)},
        "pending": {"total": pend_total, "replayable": pend_replay},
        "state": (f"memory={_bucket(mem_pct)} user={_bucket(user_pct)} "
                  f"pending={pend_total} over85={str(mem_pct >= 85).lower()}"),
    }
    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        # 打印行**绝不能带精确字符数**：monitor 对原始字节做哈希，`chars=13658` 每写一次记忆就变，
        # 门控等于没有（实测 2026-10-26~29 每次 360m 调度都唤醒、都推下游通知）。
        # 精确值只在 --json 里给脚本用；人读面按桶 + over85 就够判「该不该动手」。
        print(f"memory {out['memory']['bucket']}% over85={str(out['memory']['over85']).lower()}")
        print(f"user   {out['user']['bucket']}%")
        print(f"pending {pend_total} 批（可重放 {pend_replay}：destructive 缺 matched_entry 会被拒）")
        print(f"state  {out['state']}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="记忆占用 / 待批队列体检（确定性输出，供 monitor 门控）")
    ap.add_argument("--profile-dir", default=os.environ.get("HERMES_PROFILE_DIR") or str(_default_profile()))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    report(Path(a.profile_dir), as_json=a.json)


if __name__ == "__main__":
    main()