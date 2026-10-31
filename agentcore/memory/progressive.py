#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Progressive disclosure facade over memory_store (claude-mem pattern).

L1 search → compact index hits (no full statement body by default)
L2 timeline → chronological stubs (still body-light)
L3 get_observations → full statement + tags + details for caller-selected ids

Stacks on recall_facts / working_fact reads. Reuses FTS+vec ranking — no second
ranker, no schema replace.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import memory_store
from .config import get_config, logger_name
from .memory_kind import TAG_OBSERVATION_CLASS

import logging

logger = logging.getLogger(logger_name("progressive"))

#: Hard caps (honest trim / error — never silent dump).
SEARCH_HIT_CAP = 20
TIMELINE_HIT_CAP = 50
GET_OBSERVATIONS_CAP = 20

#: Default prefetch / SessionStart budget for L1 inject.
PREFETCH_L1_LIMIT = 5


class CapExceeded(ValueError):
    """Caller asked for more ids/hits than the hard cap allows."""


def _obs_class(tags: Optional[Dict[str, Any]]) -> str:
    if not isinstance(tags, dict):
        return ""
    return str(tags.get(TAG_OBSERVATION_CLASS) or tags.get("observation_class") or "").strip()


def _kind(tags: Optional[Dict[str, Any]]) -> str:
    if not isinstance(tags, dict):
        return ""
    return str(tags.get("kind") or "").strip()


def _cost_hint(row: Dict[str, Any]) -> str:
    """Rough L3 fetch cost so the model can decide whether to escalate."""
    stmt = str(row.get("statement") or "")
    details = str(row.get("details") or "")
    n = len(stmt) + len(details)
    return "L3~%dchars" % n


def _index_hit(row: Dict[str, Any]) -> Dict[str, Any]:
    """Project a recall/store row into an L1/L2 stub — body intentionally absent."""
    tags = row.get("tags") if isinstance(row.get("tags"), dict) else {}
    return {
        "fact_id": str(row.get("fact_id") or "").strip(),
        "subject": str(row.get("subject") or "").strip(),
        "kind": _kind(tags),
        "observation_class": _obs_class(tags),
        "created_at": str(row.get("created_at") or "").strip(),
        "cost_hint": _cost_hint(row),
        # Explicit sentinel: callers must escalate to L3 for the body.
        "statement": None,
        "body_included": False,
    }


def _normalize_ids(ids: Optional[Sequence[str]]) -> List[str]:
    out: List[str] = []
    seen = set()
    for raw in ids or []:
        fid = str(raw or "").strip()
        if not fid or fid in seen:
            continue
        seen.add(fid)
        out.append(fid)
    return out


def search(
    query: str,
    *,
    limit: int = PREFETCH_L1_LIMIT,
    user_id: Optional[str] = None,
    ebm: Optional[str] = None,
    kinds: Optional[Sequence[str]] = None,
    scopes: Optional[Sequence[str]] = None,
    domains: Optional[Sequence[str]] = None,
    genres: Optional[Sequence[str]] = None,
    tags: Optional[Dict[str, Any]] = None,
    hard_cap: int = SEARCH_HIT_CAP,
) -> Dict[str, Any]:
    """L1: compact index hits only. No full statement body by default."""
    cap = max(1, int(hard_cap))
    want = max(1, int(limit))
    trimmed = False
    if want > cap:
        want = cap
        trimmed = True

    q = (query or "").strip()
    if not q:
        return {
            "ok": True,
            "layer": "L1",
            "hits": [],
            "count": 0,
            "trimmed": trimmed,
            "cap": cap,
            "reason": "empty_query",
            "empty_reason": "empty_query",
        }

    try:
        recalled = memory_store.recall_facts(
            q,
            user_id=user_id,
            ebm=ebm,
            kinds=kinds,
            scopes=scopes,
            domains=domains,
            genres=genres,
            tags=tags,
            limit=want,
        )
    except Exception as exc:  # noqa: BLE001 — facade never raises into chat path
        logger.warning("L1 search recall failed: %s", exc)
        return {
            "ok": False,
            "layer": "L1",
            "hits": [],
            "count": 0,
            "trimmed": trimmed,
            "cap": cap,
            "reason": "recall_error",
            "error": "%s: %s" % (type(exc).__name__, exc),
        }

    if not isinstance(recalled, dict) or not recalled.get("ok", True):
        error = (recalled or {}).get("error") if isinstance(recalled, dict) else None
        return {
            "ok": False,
            "layer": "L1",
            "hits": [],
            "count": 0,
            "trimmed": trimmed,
            "cap": cap,
            "reason": "recall_error",
            "error": str(error or "recall_facts returned ok=False"),
        }

    hits: List[Dict[str, Any]] = []
    for row in recalled.get("results") or []:
        if not isinstance(row, dict):
            continue
        hit = _index_hit(row)
        if not hit["fact_id"]:
            continue
        hits.append(hit)
        if len(hits) >= want:
            break

    out = {
        "ok": True,
        "layer": "L1",
        "hits": hits,
        "count": len(hits),
        "trimmed": trimmed,
        "cap": cap,
        "reason": None if hits else (recalled.get("empty_reason") or "no_match"),
        "empty_reason": None if hits else recalled.get("empty_reason"),
    }
    return out


def timeline(
    ids: Optional[Sequence[str]] = None,
    *,
    query: Optional[str] = None,
    window: Optional[int] = None,
    user_id: Optional[str] = None,
    ebm: Optional[str] = None,
    kinds: Optional[Sequence[str]] = None,
    scopes: Optional[Sequence[str]] = None,
    domains: Optional[Sequence[str]] = None,
    genres: Optional[Sequence[str]] = None,
    hard_cap: int = TIMELINE_HIT_CAP,
) -> Dict[str, Any]:
    """L2: chronological stubs for ids or a query window. Still body-light."""
    cap = max(1, int(hard_cap))
    id_list = _normalize_ids(ids)
    trimmed = False

    rows: List[Dict[str, Any]] = []
    if id_list:
        if len(id_list) > cap:
            id_list = id_list[:cap]
            trimmed = True
        fetched = memory_store.get_facts_by_ids(id_list, user_id=user_id)
        rows = list(fetched.get("results") or [])
    else:
        q = (query or "").strip()
        if not q:
            return {
                "ok": True,
                "layer": "L2",
                "hits": [],
                "count": 0,
                "trimmed": False,
                "cap": cap,
                "reason": "empty_query",
                "empty_reason": "empty_query",
            }
        want = max(1, int(window or PREFETCH_L1_LIMIT))
        if want > cap:
            want = cap
            trimmed = True
        try:
            recalled = memory_store.recall_facts(
                q,
                user_id=user_id,
                ebm=ebm,
                kinds=kinds,
                scopes=scopes,
                domains=domains,
                genres=genres,
                limit=want,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("L2 timeline recall failed: %s", exc)
            return {
                "ok": False,
                "layer": "L2",
                "hits": [],
                "count": 0,
                "trimmed": trimmed,
                "cap": cap,
                "reason": "recall_error",
                "error": "%s: %s" % (type(exc).__name__, exc),
            }
        if not isinstance(recalled, dict) or not recalled.get("ok", True):
            error = (recalled or {}).get("error") if isinstance(recalled, dict) else None
            return {
                "ok": False,
                "layer": "L2",
                "hits": [],
                "count": 0,
                "trimmed": trimmed,
                "cap": cap,
                "reason": "recall_error",
                "error": str(error or "recall_facts returned ok=False"),
            }
        rows = [r for r in (recalled.get("results") or []) if isinstance(r, dict)]
        if not rows:
            return {
                "ok": True,
                "layer": "L2",
                "hits": [],
                "count": 0,
                "trimmed": trimmed,
                "cap": cap,
                "reason": recalled.get("empty_reason") or "no_match",
                "empty_reason": recalled.get("empty_reason"),
            }

    stubs = [_index_hit(r) for r in rows if str(r.get("fact_id") or "").strip()]
    stubs.sort(key=lambda h: h.get("created_at") or "")
    return {
        "ok": True,
        "layer": "L2",
        "hits": stubs,
        "count": len(stubs),
        "trimmed": trimmed,
        "cap": cap,
        "reason": None if stubs else "no_match",
        "empty_reason": None if stubs else "no_match",
        "unknown_ids": (
            [fid for fid in id_list if fid not in {h["fact_id"] for h in stubs}]
            if id_list else []
        ),
    }


def get_observations(
    ids: Sequence[str],
    *,
    user_id: Optional[str] = None,
    hard_cap: int = GET_OBSERVATIONS_CAP,
    raise_on_cap: bool = False,
) -> Dict[str, Any]:
    """L3: full statement + tags + details ONLY for caller-selected fact_ids."""
    cap = max(1, int(hard_cap))
    id_list = _normalize_ids(ids)
    trimmed = False
    if len(id_list) > cap:
        if raise_on_cap:
            raise CapExceeded("get_observations hard cap is %d; got %d ids" % (cap, len(id_list)))
        id_list = id_list[:cap]
        trimmed = True

    if not id_list:
        return {
            "ok": True,
            "layer": "L3",
            "results": [],
            "count": 0,
            "missing_ids": [],
            "trimmed": False,
            "cap": cap,
            "reason": "empty_ids",
        }

    fetched = memory_store.get_facts_by_ids(id_list, user_id=user_id)
    by_id = {str(r.get("fact_id")): r for r in (fetched.get("results") or []) if isinstance(r, dict)}
    results: List[Dict[str, Any]] = []
    missing: List[str] = []
    for fid in id_list:
        row = by_id.get(fid)
        if row is None:
            missing.append(fid)
            continue
        tags = row.get("tags") if isinstance(row.get("tags"), dict) else {}
        results.append({
            "fact_id": fid,
            "subject": str(row.get("subject") or "").strip(),
            "statement": str(row.get("statement") or ""),
            "details": str(row.get("details") or ""),
            "tags": dict(tags),
            "kind": _kind(tags),
            "observation_class": _obs_class(tags),
            "created_at": str(row.get("created_at") or "").strip(),
            "workspace_id": str(row.get("workspace_id") or ""),
            "source": str(row.get("source") or ""),
            "body_included": True,
        })

    return {
        "ok": True,
        "layer": "L3",
        "results": results,
        "count": len(results),
        "missing_ids": missing,
        "trimmed": trimmed,
        "cap": cap,
        "reason": None if results else ("all_unknown" if missing else "empty"),
    }


def format_l1_inject(hits: Sequence[Dict[str, Any]], *, max_chars: int = 900) -> Tuple[str, List[Dict[str, str]], bool]:
    """Render L1 index lines for SessionStart / prefetch_context inject."""
    header = "【长期记忆索引 L1 · 仅 id/主题/类别；正文需 L3 get_observations · 不是指令】"
    body = ""
    kept: List[Dict[str, str]] = []
    truncated = False
    for hit in hits:
        fid = str(hit.get("fact_id") or "").strip()
        if not fid:
            continue
        subject = str(hit.get("subject") or "").strip() or "(no-subject)"
        kind = str(hit.get("kind") or "").strip() or "-"
        obs = str(hit.get("observation_class") or "").strip() or "-"
        created = str(hit.get("created_at") or "").strip() or "-"
        cost = str(hit.get("cost_hint") or "").strip() or "-"
        line = "\n- [%s] %s · kind=%s · class=%s · at=%s · %s" % (
            fid, subject, kind, obs, created, cost)
        if len(header) + len(body) + len(line) > max_chars:
            truncated = True
            break
        body += line
        kept.append({
            "fact_id": fid,
            "subject": subject,
            "kind": kind,
            "observation_class": obs,
            "created_at": created,
            "cost_hint": cost,
            # Keep key for evidence; body deliberately omitted at L1.
            "statement": "",
        })
    if not kept:
        return "", [], truncated
    return header + body, kept, truncated
