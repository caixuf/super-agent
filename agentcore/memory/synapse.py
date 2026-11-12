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
import math
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

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
MAX_USE_TIMESTAMPS = 32
COACTIVATION_STEP = 0.15
MAX_COACTIVATION_EDGES = 24


def _store_path() -> Path:
    """账本路径：宿主显式指定优先，否则放记忆库同目录。

    宿主历史账本可能在别处，路径必须可注入 —— 换了路径不报错，
    只会让所有已积累的权重静默归零。
    """
    cfg = get_config()
    return cfg.synapse_store_path()


#: 读路径缓存。召回时每次都要读这份账本，5k 条 JSON 解析约 3.2ms；
#: 账本只在写入时变化，按 (path, mtime_ns, size, inode) 失效即可。
_LOAD_CACHE_KEY = None
_LOAD_CACHE_DATA: Optional[Dict[str, Dict[str, Any]]] = None
_LOAD_CACHE_LOCK = threading.Lock()


def _cache_key(path: Path):
    try:
        info = path.stat()
    except OSError:
        return None
    return (str(path), info.st_mtime_ns, info.st_size, info.st_ino)


def _parse(path: Path) -> Dict[str, Dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001 —— 账本坏了退默认权重，不阻断召回
        logger.debug("读取突触账本失败: %s", exc)
        return {}


def _load() -> Dict[str, Dict[str, Any]]:
    """只读快照：调用方不得修改返回值。"""
    p = _store_path()
    key = _cache_key(p)
    if key is None:
        return {}
    global _LOAD_CACHE_KEY, _LOAD_CACHE_DATA
    with _LOAD_CACHE_LOCK:
        if _LOAD_CACHE_KEY == key and _LOAD_CACHE_DATA is not None:
            return _LOAD_CACHE_DATA
    data = _parse(p)
    with _LOAD_CACHE_LOCK:
        _LOAD_CACHE_KEY = key
        _LOAD_CACHE_DATA = data
    return data


def _load_mutable() -> Dict[str, Dict[str, Any]]:
    """写入路径必须拿独立副本：改完再 _save，不能污染只读缓存。"""
    return _parse(_store_path())


def _save(synapses: Dict[str, Dict[str, Any]]) -> bool:
    p = _store_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(synapses, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)  # 原子替换：崩在写一半也不会留下截断的账本
        global _LOAD_CACHE_KEY, _LOAD_CACHE_DATA
        with _LOAD_CACHE_LOCK:
            _LOAD_CACHE_KEY = None
            _LOAD_CACHE_DATA = None
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("保存突触账本失败: %s", exc)
        return False


def load_synapses() -> Dict[str, Dict[str, Any]]:
    """公共账本快照（可修改后保存）：语义与旧实现一致，始终返回新副本。"""
    return _load_mutable()


def load_synapses_cached() -> Dict[str, Dict[str, Any]]:
    """热召回路径专用的只读快照；调用方不得修改返回值。"""
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

    synapses = _load_mutable()
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
        # 记录 ACT-R 激活时间戳（保留最近 MAX_USE_TIMESTAMPS 次）
        ts_list = [float(x) for x in (entry.get("use_timestamps") or []) if isinstance(x, (int, float))]
        ts_list.append(round(time.time(), 3))
        entry["use_timestamps"] = ts_list[-MAX_USE_TIMESTAMPS:]
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


def actr_activation(
    entry: Dict[str, Any],
    *,
    now: Optional[float] = None,
    decay: float = 0.5,
) -> float:
    """计算 ACT-R 幂律基础激活度 B_i = ln(sum_{k} max(1.0, dt_hours)^(-d))。

    未记录过时间戳时返回 0.0（中性基准）。
    """
    ts_list = [float(x) for x in (entry.get("use_timestamps") or []) if isinstance(x, (int, float))]
    if not ts_list:
        rcount = int(entry.get("reinforce_count") or 0)
        if rcount <= 0:
            return 0.0
        # 兼容历史没有 use_timestamps 的强化条目
        return round(math.log(max(1.0, float(rcount))), 4)
    current = float(now if now is not None else time.time())
    power_sum = 0.0
    for ts in ts_list:
        dt_hours = max(0.1, (current - ts) / 3600.0)
        power_sum += math.pow(dt_hours, -max(0.1, float(decay)))
    if power_sum <= 0.0:
        return 0.0
    return round(math.log(power_sum), 4)


def actr_weights_for(
    fact_ids: Sequence[Optional[str]],
    *,
    now: Optional[float] = None,
    decay: float = 0.5,
) -> Dict[str, float]:
    """融合突触状态权重、置信度与 ACT-R 幂律时间衰减的综合排序乘子。

    返回值范围约在 [0.0, 1.35]，默认未记录事实为 1.0。
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
            out[fid] = 1.0
            continue
        status = str(entry.get("status") or STATUS_ACTIVE)
        base_w = STATUS_WEIGHT.get(status, 1.0)
        if base_w <= 0.0:
            out[fid] = 0.0
            continue
        conf = float(entry.get("confidence", DEFAULT_CONFIDENCE))
        conf_ratio = conf / DEFAULT_CONFIDENCE
        b_i = actr_activation(entry, now=now, decay=decay)
        # Sigmoid 映射 B_i 到 [0.82, 1.18] 的平滑调节系数
        actr_factor = 0.82 + 0.36 / (1.0 + math.exp(-0.5 * b_i))
        out[fid] = round(base_w * conf_ratio * actr_factor, 4)
    return out


def record_coactivation(
    fact_ids: Sequence[str],
    *,
    success: bool = True,
    step: float = COACTIVATION_STEP,
) -> Dict[str, Any]:
    """进化二：赫布突触学习律（Cells that fire together, wire together）。

    当多条事实在同一次任务中被联合召回并促成任务成功时，增强它们之间的双向共激活边权重 w_AB。
    """
    unique_ids = [str(f).strip() for f in dict.fromkeys(fact_ids) if str(f or "").strip()]
    if len(unique_ids) < 2:
        return {"ok": True, "edges_updated": 0}
    synapses = _load_mutable()
    delta = float(step) if success else -0.5 * float(step)
    updated_pairs = 0
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")

    for i, fid_a in enumerate(unique_ids):
        entry_a = synapses.get(fid_a) or {
            "id": fid_a,
            "confidence": DEFAULT_CONFIDENCE,
            "reinforce_count": 0,
            "penalty_count": 0,
            "consecutive_failures": 0,
            "targets": [],
            "status": STATUS_ACTIVE,
        }
        edges_a: Dict[str, float] = dict(entry_a.get("co_edges") or {})
        for j, fid_b in enumerate(unique_ids):
            if i == j:
                continue
            old_w = float(edges_a.get(fid_b, 0.0))
            new_w = round(min(1.0, max(0.0, old_w + delta)), 4)
            if new_w > 0.02:
                edges_a[fid_b] = new_w
            else:
                edges_a.pop(fid_b, None)
            if i < j:
                updated_pairs += 1
        # 限制单节点最大边数，保留权重最高的 MAX_COACTIVATION_EDGES 条边
        if len(edges_a) > MAX_COACTIVATION_EDGES:
            top_items = sorted(edges_a.items(), key=lambda kv: -kv[1])[:MAX_COACTIVATION_EDGES]
            edges_a = dict(top_items)
        entry_a["co_edges"] = edges_a
        entry_a["last_updated"] = now_str
        synapses[fid_a] = entry_a

    _save(synapses)
    return {"ok": True, "edges_updated": updated_pairs, "nodes": len(unique_ids)}


def spreading_activation(
    seed_fact_ids: Sequence[str],
    *,
    top_k: int = 3,
    min_weight: float = 0.15,
) -> List[Dict[str, Any]]:
    """进化二：1-Hop 赫布突触扩散激活（Spreading Activation）。

    给定首轮召回命中的种子事实 ID 列表，沿共激活图谱向外扩散 1 跳，
    捞出虽未直接命中字面查询、但历史上经常与种子事实共同解决问题的伴生事实。
    """
    seeds = [str(f).strip() for f in dict.fromkeys(seed_fact_ids) if str(f or "").strip()]
    if not seeds or top_k < 1:
        return []
    synapses = _load()
    seed_set = set(seeds)
    candidate_scores: Dict[str, float] = {}
    via_map: Dict[str, List[str]] = {}

    for s_id in seeds:
        s_entry = synapses.get(s_id)
        if not s_entry or s_entry.get("status") == STATUS_DEPRECATED:
            continue
        co_edges = s_entry.get("co_edges") or {}
        if not isinstance(co_edges, dict):
            continue
        for target_id, edge_w in co_edges.items():
            if not target_id or target_id in seed_set:
                continue
            w = float(edge_w or 0.0)
            if w < min_weight:
                continue
            t_entry = synapses.get(target_id) or {}
            t_status = str(t_entry.get("status") or STATUS_ACTIVE)
            if t_status == STATUS_DEPRECATED:
                continue
            t_conf = float(t_entry.get("confidence", DEFAULT_CONFIDENCE))
            status_mult = STATUS_WEIGHT.get(t_status, 1.0)
            contrib = w * (t_conf / DEFAULT_CONFIDENCE) * status_mult
            candidate_scores[target_id] = round(candidate_scores.get(target_id, 0.0) + contrib, 4)
            via_map.setdefault(target_id, []).append(s_id)

    ranked = sorted(candidate_scores.items(), key=lambda kv: (-kv[1], kv[0]))[: int(top_k)]
    return [
        {
            "fact_id": fid,
            "spread_score": score,
            "activated_by": via_map.get(fid, []),
        }
        for fid, score in ranked
    ]


def merge_synapse(old_id: str, new_id: str) -> Dict[str, Any]:
    """在睡眠巩固归并碎片时，将旧事实的突触强化计数、时间戳与共激活边无损转移给新事实。"""
    old_k = (old_id or "").strip()
    new_k = (new_id or "").strip()
    if not old_k or not new_k or old_k == new_k:
        return {"ok": False, "merged": False}
    synapses = _load_mutable()
    old_entry = synapses.pop(old_k, None)
    if not old_entry:
        return {"ok": True, "merged": False}
    new_entry = synapses.get(new_k) or {
        "id": new_k,
        "confidence": DEFAULT_CONFIDENCE,
        "reinforce_count": 0,
        "penalty_count": 0,
        "consecutive_failures": 0,
        "targets": [],
        "status": STATUS_ACTIVE,
    }
    new_entry["confidence"] = round(
        max(float(new_entry.get("confidence", DEFAULT_CONFIDENCE)),
            float(old_entry.get("confidence", DEFAULT_CONFIDENCE))),
        2,
    )
    new_entry["reinforce_count"] = int(new_entry.get("reinforce_count", 0)) + int(old_entry.get("reinforce_count", 0))
    ts_merged = sorted(
        set(
            [float(x) for x in (old_entry.get("use_timestamps") or []) if isinstance(x, (int, float))]
            + [float(x) for x in (new_entry.get("use_timestamps") or []) if isinstance(x, (int, float))]
        )
    )
    if ts_merged:
        new_entry["use_timestamps"] = ts_merged[-MAX_USE_TIMESTAMPS:]

    edges_new: Dict[str, float] = dict(new_entry.get("co_edges") or {})
    for target_id, w in (old_entry.get("co_edges") or {}).items():
        if target_id == new_k:
            continue
        edges_new[target_id] = max(float(edges_new.get(target_id, 0.0)), float(w or 0.0))
        if target_id in synapses and isinstance(synapses[target_id].get("co_edges"), dict):
            peer_edges = synapses[target_id]["co_edges"]
            old_peer_w = float(peer_edges.pop(old_k, 0.0))
            if old_peer_w > 0:
                peer_edges[new_k] = max(float(peer_edges.get(new_k, 0.0)), old_peer_w)
    if edges_new:
        new_entry["co_edges"] = edges_new
    synapses[new_k] = new_entry
    _save(synapses)
    return {"ok": True, "merged": True, "from": old_k, "to": new_k}


def drop(pattern_id: str) -> Dict[str, Any]:
    """彻底移除一条账本记录（事实被删除时同步调用）。"""
    synapses = _load_mutable()
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
