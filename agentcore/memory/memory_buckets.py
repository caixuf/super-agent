#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分桶维度的兼容层。取值已迁到可扩展注册表（memory_registry）。

关键变化（2026-10-23 第三次修正，用户指出）：
    原来 DOMAINS / GENRES 是写死的元组 —— 每加一个领域都要改代码发版，
    等于把「记忆系统的自动化」又破坏了：系统能自动学习，却学不出新分类。
    现在取值全部来自 SQLite 注册表，新领域自动登记，代码不用动。

唯一保留的业务判断：哪些领域带代际。这是「只有这个领域的行为才有 G1/G2」
这条业务规则，不是可自动推导的分类数据，且存在注册表 has_ebm 列里。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence, Tuple

DEFAULT_DOMAIN = "general"
DEFAULT_GENRE = "experience"

SCOPE_TO_GENRE = {"platform": "experience", "reference": "reference", "ops": "procedure"}
KIND_TO_DOMAIN = {"technical": "general", "personal": "people", "context": "ops"}


def _reg():
    try:
        from . import memory_registry
    except ImportError:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from . import memory_registry  # type: ignore
    return memory_registry


def domains(only_active: bool = True):
    return _reg().domains(only_active=only_active)


def genres(only_active: bool = True):
    return _reg().genres(only_active=only_active)


def normalize_domain(domain: Optional[str]) -> str:
    return _reg().normalize("domain", domain)


def normalize_genre(genre: Optional[str]) -> str:
    return _reg().normalize("genre", genre)


def has_ebm(domain: Optional[str]) -> bool:
    return _reg().has_ebm(domain)


def register_dimensions(domain: Optional[str] = None,
                        genre: Optional[str] = None,
                        ebm: Optional[str] = None) -> Dict[str, str]:
    """把维度归一成待写入的标签。ebm 只对带代际的领域记录。"""
    dom = normalize_domain(domain)
    out = {"domain": dom, "genre": normalize_genre(genre)}
    if has_ebm(dom):
        out["ebm"] = (ebm or "").strip() or "_unknown"
    return out


def filter_clause(dimension: str, values: Optional[Sequence[str]]) -> Tuple[str, list]:
    return _reg().filter_clause(dimension, values)


def describe() -> Dict[str, Any]:
    return _reg().describe()
