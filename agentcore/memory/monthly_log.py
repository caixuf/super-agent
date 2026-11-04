#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Needle-class awareness → monthly markdown append (no Feishu / model turn).

On admitted write of observation_class ∈ {decision, bugfix, security_alert,
sensitive}, append ONE line to <MemoryConfig.base_dir()>/memory/log/YYYY-MM.md
(Asia/Shanghai calendar month). Idempotent by fact_id.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from .config import get_config, logger_name
from .memory_kind import (
    NEEDLE_CLASSES,
    TAG_OBSERVATION_CLASS,
    classify_observation_class,
    is_needle,
)

logger = logging.getLogger(logger_name("monthly_log"))

SHANGHAI = ZoneInfo("Asia/Shanghai")
STATEMENT_MAX = 160
_ID_RE = re.compile(r"id=([^\s]+)\s*$")


def _now_shanghai(when: Optional[datetime] = None) -> datetime:
    if when is None:
        return datetime.now(SHANGHAI)
    if when.tzinfo is None:
        return when.replace(tzinfo=SHANGHAI)
    return when.astimezone(SHANGHAI)


def monthly_log_dir() -> Path:
    return get_config().base_dir() / "memory" / "log"


def monthly_log_path(when: Optional[datetime] = None) -> Path:
    stamp = _now_shanghai(when)
    return monthly_log_dir() / ("%04d-%02d.md" % (stamp.year, stamp.month))


def _one_line(statement: str, limit: int = STATEMENT_MAX) -> str:
    text = re.sub(r"\s+", " ", (statement or "").strip())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass


def _fact_ids_in_file(path: Path) -> set:
    if not path.is_file():
        return set()
    found = set()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    for line in text.splitlines():
        m = _ID_RE.search(line.strip())
        if m:
            found.add(m.group(1))
    return found


def resolve_observation_class(
    statement: str = "",
    subject: str = "",
    tags: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    return classify_observation_class(statement, subject, tags)


def append_needle(
    *,
    fact_id: str,
    observation_class: str,
    subject: str,
    statement: str,
    when: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Append one monthly line for a needle fact. Idempotent by fact_id."""
    fid = str(fact_id or "").strip()
    obs = str(observation_class or "").strip().lower()
    if not fid:
        return {"ok": False, "appended": False, "reason": "missing_fact_id"}
    if not is_needle(obs):
        return {"ok": True, "appended": False, "reason": "non_needle", "observation_class": obs}

    stamp = _now_shanghai(when)
    path = monthly_log_path(stamp)
    _ensure_parent(path)

    if fid in _fact_ids_in_file(path):
        return {
            "ok": True,
            "appended": False,
            "reason": "duplicate_fact_id",
            "path": str(path),
            "fact_id": fid,
            "observation_class": obs,
        }

    line = "- %04d-%02d-%02d %02d:%02d · %s · %s · %s · id=%s\n" % (
        stamp.year, stamp.month, stamp.day, stamp.hour, stamp.minute,
        obs,
        (subject or "").strip() or "(no-subject)",
        _one_line(statement),
        fid,
    )
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        logger.warning("monthly log append failed: %s", exc)
        return {"ok": False, "appended": False, "reason": "io_error", "error": str(exc)}

    return {
        "ok": True,
        "appended": True,
        "reason": None,
        "path": str(path),
        "fact_id": fid,
        "observation_class": obs,
        "line": line.rstrip("\n"),
    }


def maybe_append_from_write(
    write_result: Dict[str, Any],
    *,
    statement: str = "",
    subject: str = "",
    tags: Optional[Dict[str, Any]] = None,
    when: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Hook after write_fact / memory_trigger success.

    Only fires when written=True (admitted new write). Never Feishu / IM / model.
    """
    if not isinstance(write_result, dict):
        return {"ok": True, "appended": False, "reason": "no_write_result"}
    if not write_result.get("written"):
        return {"ok": True, "appended": False, "reason": "not_written"}

    fid = str(write_result.get("fact_id") or "").strip()
    merged_tags = {}
    if isinstance(tags, dict):
        merged_tags.update(tags)
    existing = write_result.get("tags")
    if isinstance(existing, dict):
        merged_tags.update(existing)

    obs = resolve_observation_class(statement, subject, merged_tags)
    if not obs:
        return {"ok": True, "appended": False, "reason": "non_needle", "fact_id": fid}

    return append_needle(
        fact_id=fid,
        observation_class=obs,
        subject=subject,
        statement=statement,
        when=when,
    )
