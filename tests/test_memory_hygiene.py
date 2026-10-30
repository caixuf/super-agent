"""Unit tests for agentcore.memory_hygiene."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from agentcore.memory_hygiene import (
    MemoryHygieneAuditor,
    audit_memory_file,
    bigram_jaccard,
    compact_memory_file,
)


def test_bigram_jaccard():
    assert bigram_jaccard("", "") == 1.0
    assert bigram_jaccard("abc", "abc") == 1.0
    assert bigram_jaccard("apple", "banana") == 0.0

    s1 = "代理统一为 127.0.0.1:7899；旧的 7897 已失效。"
    s2 = "代理统一为 127.0.0.1:7899；旧的 7897 已经失效。"
    sim = bigram_jaccard(s1, s2)
    assert sim > 0.80


def test_memory_hygiene_audit_capacity():
    auditor = MemoryHygieneAuditor(max_characters=1000)

    # 1. Healthy (under 70% / 700 chars)
    healthy_text = "代理配置规则：走本地 7899 端口。\n§\n主模型规则：走 main 分组。"
    res = auditor.audit(healthy_text)
    assert res["status"] == "healthy"
    assert res["healthy"] is True
    assert res["warning"] is False
    assert res["usage_percent"] < 70.0

    # 2. Warning (between 700 and 849 chars)
    warn_text = "A" * 750
    res_warn = auditor.audit(warn_text)
    assert res_warn["status"] == "warning"
    assert res_warn["warning"] is True

    # 3. Critical (>= 850 chars)
    crit_text = "B" * 900
    res_crit = auditor.audit(crit_text)
    assert res_crit["status"] == "critical"
    assert res_crit["warning"] is True


def test_memory_hygiene_duplicate_detection():
    auditor = MemoryHygieneAuditor(max_characters=5000)
    sections = [
        "代理统一为 127.0.0.1:7899；旧的 7897 已失效。",
        "主模型走 main 分组，跟随窗口切换。",
        "代理统一为 127.0.0.1:7899；旧的 7897 已经失效。",  # Near-duplicate of #0
    ]
    dups = auditor.find_duplicates(sections, threshold=0.85)
    assert len(dups) == 1
    assert dups[0]["index_a"] == 0
    assert dups[0]["index_b"] == 2
    assert dups[0]["similarity"] >= 0.85


def test_memory_hygiene_compaction():
    auditor = MemoryHygieneAuditor(max_characters=5000)
    raw_memory = """代理统一为 127.0.0.1:7899；旧的 7897 已失效。
§
## 临时任务记录
用户刚刚测试了雨刮任务，临时输出在 /tmp/wiper_test，后续可清理。
§
主模型走 main 分组，跟随窗口切换。
§
代理统一为 127.0.0.1:7899；旧的 7897 已经失效。
"""
    compacted, summary = auditor.compact(raw_memory)
    assert summary["pruned_temporary_sections"] == 1
    assert summary["pruned_duplicate_sections"] == 1
    assert summary["saved_characters"] > 0
    assert "临时任务记录" not in compacted
    assert "主模型走 main 分组" in compacted


def test_audit_and_compact_file(tmp_path: Path):
    mem_file = tmp_path / "MEMORY.md"
    mem_file.write_text(
        "核心规则 1：零 Emoji 守卫。\n§\n## 临时草稿\n测试跑测日志尾部...\n§\n核心规则 1：零 Emoji 守卫。\n",
        encoding="utf-8",
    )

    audit_res = audit_memory_file(mem_file, max_characters=2000)
    assert audit_res["ok"] is True
    assert audit_res["section_count"] == 3
    assert audit_res["temporary_sections_count"] == 1
    assert audit_res["duplicate_pairs_count"] == 1

    compact_res = compact_memory_file(mem_file, backup=True, max_characters=2000)
    assert compact_res["ok"] is True
    assert compact_res["pruned_temporary_sections"] == 1
    assert compact_res["pruned_duplicate_sections"] == 1
    assert Path(compact_res["backup_file"]).is_file()

    new_content = mem_file.read_text(encoding="utf-8")
    assert "临时草稿" not in new_content
    assert "核心规则 1" in new_content
