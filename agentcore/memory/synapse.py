"""突触可塑性账本：按真实使用结果动态调整记忆的权重与状态。

为什么需要独立的账本而不是直接改事实表的置信度：

    一条记忆「有没有用」只有在被真实使用之后才知道。而这个信号是**时序性**的
    ——先召回、再执行、最后才拿到成败。把它塞回事实表意味着要跨越三个阶段的
    写事务，任何一步失败都会让置信度与事实脱节。

    独立账本 + 按 fact_id 关联是更稳的形状：账本可丢（丢了只是退回到默认权重），
    事实表不可丢。

状态机（v2 口径）：

    active   ──连续失败 >= suspect_after──▶  suspect  ──一次成功──▶ active
    suspect  ──持续失败──▶ suspect（不再降）

    * 失败只降权（乘性），不直接判死。降级的判据是「连续错」，不是「历史累计错过」。
    * 一次成功即清零连续失败并回到 active —— 置信度不该当开关用。
      历史死锁：5 次失败把 confidence 压到 0.4，一次 +0.05 到 0.45，
      永远够不到 0.5 的门槛，于是这条记忆再也回不来。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import get_config, logger_name

logger = logging.getLogger(logger_name("synapse"))

STATUS_ACTIVE = "active"
STATUS_SUSPECT = "suspect"
STATUS_DEPRECATED = "deprecated"

#: 召回时按状态乘的权重。数字是被实测逼出来的：
#: 0.85 压不动一条相似度差 0.24 的条目（0.8756×0.85=0.744 仍高于 0.6303），
#: 等于降级白做。suspect 给 0.5 才能跌出 top-1 但不至于消失。
STATUS_WEIGHT = {STATUS_ACTIVE: 1.0, STATUS_SUSPECT: 0.5, STATUS_DEPRECATED: 0.0}

DEFAULT_CONFIDENCE = 0.85
REINFORCE_STEP = 0.05
PENALIZE_STEP = 0.15
MIN_CONFIDENCE = 0.1
SUSPECT_AFTER = 5


def _store_path() -> Path:
    """账本路径：宿主显式指定优先，否则放记忆库同目录。

    宿主历史账本可能在别处，路径必须可注入 —— 换了路径不报错，
    只会让所有已积累的权重静默归零。
    """
    cfg = get_config()
    return cfg.synapse_store_path()


def _load() -> Dict[str, Dict[str, Any]]:
    p = _store_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001 —— 账本坏了退默认权重，不阻断召回
        logger.debug("读取突触账本失败: %s", exc)
        return {}


def _save(synapses: Dict[str, Dict[str, Any]]) -> bool:
    p = _store_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(synapses, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)  # 原子替换：崩在写一半也不会留下截断的账本
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("保存突触账本失败: %s", exc)
        return False


def load_synapses() -> Dict[str, Dict[str, Any]]:
    return _load()


def weights_for(fact_ids: List[Optional[str]]) -> Dict[str, float]:
    """按账本状态给出每条事实的排序权重。

    没记录的一律 1.0 —— 没被用过不等于不可信。
    """
    out: Dict[str, float] = {}
    try:
        syn = _load()
    except Exception:  # noqa: BLE001
        return out
    for fid in fact_ids:
        if not fid:
            continue
        entry = syn.get(fid)
        if not entry:
            continue
        status = str(entry.get("status") or STATUS_ACTIVE)
        out[fid] = STATUS_WEIGHT.get(status, 1.0)
    return out


def reinforce(pattern_id: str, success: bool, target: str = "",
              reason: str = "") -> Dict[str, Any]:
    """记录一次真实使用结果，调整该记忆的置信度与状态。

    参数：
      pattern_id: 事实 ID（或调用方自定义的稳定键）
      success:    这次使用是否成功
      target:     成功时打上的标签（如版本号）；空则不记
      reason:     失败原因，截断保存
    """
    key = (pattern_id or "").strip()
    if not key:
        return {"ok": False, "error": "pattern_id 不能为空"}

    synapses = _load()
    entry = synapses.get(key) or {
        "id": key,
        "confidence": DEFAULT_CONFIDENCE,
        "reinforce_count": 0,
        "penalty_count": 0,
        "consecutive_failures": 0,
        "targets": [],
        "status": STATUS_ACTIVE,
    }

    if success:
        entry["confidence"] = round(
            min(1.0, float(entry.get("confidence", DEFAULT_CONFIDENCE)) + REINFORCE_STEP), 2)
        entry["reinforce_count"] = int(entry.get("reinforce_count", 0)) + 1
        entry["consecutive_failures"] = 0
        if target and target not in entry.get("targets", []):
            entry.setdefault("targets", []).append(target)
        # 成功一次就回 active：连续失败已清零，再用置信度门槛卡住会造成死锁。
        entry["status"] = STATUS_ACTIVE
    else:
        entry["confidence"] = round(
            max(MIN_CONFIDENCE, float(entry.get("confidence", DEFAULT_CONFIDENCE)) - PENALIZE_STEP), 2)
        entry["penalty_count"] = int(entry.get("penalty_count", 0)) + 1
        entry["consecutive_failures"] = int(entry.get("consecutive_failures", 0)) + 1
        entry["last_penalty_reason"] = (reason or "")[:200]
        # 只降权不删除：知识丢了就找不回来，排到后面只是暂时不用它。
        if entry["consecutive_failures"] >= SUSPECT_AFTER:
            entry["status"] = STATUS_SUSPECT

    entry["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    synapses[key] = entry
    _save(synapses)

    return {
        "ok": True,
        "id": key,
        "confidence": entry["confidence"],
        "status": entry["status"],
        "reinforce_count": entry["reinforce_count"],
        "penalty_count": entry["penalty_count"],
        "consecutive_failures": entry["consecutive_failures"],
        "targets": entry.get("targets", []),
        "action": "reinforced" if success else "penalized",
    }


def drop(pattern_id: str) -> Dict[str, Any]:
    """彻底移除一条账本记录（事实被删除时同步调用）。"""
    synapses = _load()
    existed = synapses.pop((pattern_id or "").strip(), None) is not None
    if existed:
        _save(synapses)
    return {"ok": True, "removed": existed}


def stats() -> Dict[str, Any]:
    synapses = _load()
    by_status: Dict[str, int] = {}
    for e in synapses.values():
        s = str(e.get("status") or STATUS_ACTIVE)
        by_status[s] = by_status.get(s, 0) + 1
    return {
        "path": str(_store_path()),
        "total": len(synapses),
        "by_status": by_status,
        "avg_confidence": round(
            sum(float(e.get("confidence", DEFAULT_CONFIDENCE)) for e in synapses.values())
            / len(synapses), 3) if synapses else 0.0,
    }
