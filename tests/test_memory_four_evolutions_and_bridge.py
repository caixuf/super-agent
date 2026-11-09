#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端测试：海马体四大进化 + 第0步信噪比隔离 + 大模型双向神经桥打通。"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import time
import types

import pytest

from agentcore.memory import (
    MemoryConfig,
    apply_task_outcome,
    check_fact_freshness,
    compute_surprise,
    configure,
    consolidate_memories,
    extract_causal_triplet,
    flush_background,
    recall_facts,
    recall_hybrid,
    reset_config,
    should_flash_write,
    write_fact,
)
from agentcore.memory.entity_store import EntityStore
from agentcore.memory import memory_trace, synapse

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated_memory(tmp_path):
    data_dir = tmp_path / "mem_data"
    data_dir.mkdir(mode=0o700)
    entity_db = tmp_path / "entity_rules.db"
    syn_file = data_dir / "memory_synapses.json"

    def _fake_embed(texts):
        vecs = []
        for t in texts:
            low = t.lower()
            v = [0.01] * 16
            if "timeout" in low or "超时" in low:
                v[0] = 0.95
            if "pool.py" in low or "worker" in low:
                v[1] = 0.95
            if "dds" in low or "零拷贝" in low:
                v[2] = 0.95
            if "textbook" in low or "教材" in low:
                v[0] = 0.85
                v[3] = 0.50
            vecs.append(v)
        return vecs

    cfg = MemoryConfig(
        data_dir=data_dir,
        default_user="u_test",
        default_agent="a_test",
        embed_dim=16,
        embed_fn=_fake_embed,
        synapse_path=syn_file,
    )
    configure(cfg)
    try:
        yield {
            "data_dir": data_dir,
            "entity_db": entity_db,
            "syn_file": syn_file,
            "cfg": cfg,
        }
    finally:
        flush_background(timeout=5.0)
        reset_config()


def test_step0_reference_noise_isolation_and_bookmark_pinning(isolated_memory):
    """验证第0步：默认隔离 reference 电子书噪音，且 file:/error: 书签精确置顶。"""
    entity_db = isolated_memory["entity_db"]
    # 写入多条电子书 reference 噪音
    for idx in range(5):
        write_fact(
            f"Python 教材章节 {idx} 讨论通用 socket timeout 超时基础概念",
            subject=f"教材_{idx}",
            source="book_ingest",
            user_id="u_test",
            agent_id="a_test",
            tags={"profile": "p_test", "scope": "reference"},
            schedule_background_index=False,
        )
    # 写入 1 条核心项目经验，带 file 与 error 书签
    core = write_fact(
        "修改 pool.py 的 worker 超时必须同步调整 lease_seconds",
        subject="worker_timeout",
        source="auto_learn",
        user_id="u_test",
        agent_id="a_test",
        tags={
            "profile": "p_test",
            "scope": "platform",
            "file": "agentcore/pool.py",
            "error": "worker_lease_timeout",
            "polarity": "prohibition",
        },
        schedule_background_index=False,
    )
    assert core["written"] is True

    # 默认 recall_hybrid 隔离 reference 书本，只返回核心经验与实体规则
    res = recall_hybrid(
        "排查 pool.py 的 worker_lease_timeout 超时问题",
        limit=5,
        entity_db=entity_db,
        user_id="u_test",
        agent_id="a_test",
        profile="p_test",
    )
    assert res["ok"] is True
    stmts = [r["statement"] for r in res["results"]]
    assert any("修改 pool.py 的 worker 超时" in s for s in stmts)
    assert all("Python 教材章节" not in s for s in stmts)
    # 命中 file:/error: 书签的记录必须置顶在第 1 位且标有 bookmark_pinned
    top_hit = res["results"][0]
    assert "修改 pool.py 的 worker 超时" in top_hit["statement"]
    assert top_hit.get("bookmark_pinned") is True


def test_evolution1_semantic_surprise_llm_and_embedding(isolated_memory):
    """验证进化一：通过向量余弦新颖度 + 语义大模型（llm_fn）计算惊奇度并提炼四格错题卡。"""
    llm_calls = []

    def _mock_llm(messages, **kwargs):
        llm_calls.append(messages)
        return {
            "content": json.dumps(
                {
                    "is_noise_or_question": False,
                    "is_mandate": True,
                    "is_belief_flip": True,
                    "is_causal_fix": True,
                    "polarity": "prohibition",
                    "subject": "colleague_worker.py",
                    "statement": "禁止在 colleague_worker.py 中硬编码模型名，统一通过 runner.json 配置",
                    "when": "file:agentcore/colleague_worker.py | error:model_hardcode_error",
                    "do": "禁止硬编码模型名，必须读取 runner.json",
                    "why": "硬编码会导致模型切换断路",
                    "files": ["agentcore/colleague_worker.py"],
                    "errors": ["model_hardcode_error"],
                },
                ensure_ascii=False,
            )
        }

    isolated_memory["cfg"].llm_fn = _mock_llm
    user_msg = "不对！记住：以后在 agentcore/colleague_worker.py 里面绝对不要硬编码模型名，遇到 model_hardcode_error 统一改用 runner.json 配置！"
    surprise = compute_surprise(
        user_msg,
        existing_statements=["DDS 零拷贝共享内存队列需要 64 字节对齐"],
        user_message=user_msg,
    )
    assert surprise["by"] == "llm"
    assert len(llm_calls) == 1
    assert surprise["novelty"] > 0.5
    assert surprise["flash_eligible"] is True
    assert surprise["admit_eligible"] is True
    assert "agentcore/colleague_worker.py" in surprise["bookmarks"]["files"]
    assert "model_hardcode_error" in surprise["bookmarks"]["errors"]

    triplet = extract_causal_triplet(user_msg, "已收到并按 runner.json 修复")
    assert triplet is not None
    assert triplet["polarity"] == "prohibition"
    assert triplet["subject"] == "colleague_worker.py"
    assert "禁止在 colleague_worker.py 中硬编码模型名" in triplet["statement"]
    assert should_flash_write(user_msg) is True


def test_evolution2_hebbian_coactivation_and_1hop_spreading(isolated_memory):
    """验证进化二：任务成功自动建立赫布共激活突触边 w_AB，且混合召回 1-Hop 扩散激活伴生事实 B。"""
    f_a = write_fact(
        "排查 worker 超时需要先检查 heartbeat 心跳间隔",
        subject="worker_timeout",
        source="auto_learn",
        user_id="u_test",
        agent_id="a_test",
        tags={"profile": "p_test", "scope": "platform"},
        schedule_background_index=False,
    )["fact_id"]
    f_b = write_fact(
        "调整 liveness.db 的 WAL 锁超时可消除队列假死",
        subject="liveness_wal_tuning",
        source="auto_learn",
        user_id="u_test",
        agent_id="a_test",
        tags={"profile": "p_test", "scope": "platform"},
        schedule_background_index=False,
    )["fact_id"]

    # 模拟一次任务同时使用了 f_a 和 f_b 并成功完成
    memory_trace.record([f_a, f_b], task_id="task_hebb_01", channel="test", query="worker timeout")
    outcome = apply_task_outcome("task_hebb_01", passed=True, target="v1")
    assert outcome["ok"] is True
    assert outcome["coactivated_edges"] >= 1

    # 验证赫布扩散激活：只传入种子 f_a，自动激活伴生经验 f_b
    spread = synapse.spreading_activation([f_a], top_k=3)
    assert any(item["fact_id"] == f_b for item in spread)

    # 在混合召回中只问 "worker 超时"（字面和向量都不含 liveness_wal_tuning），
    # 1-Hop 扩散激活应自动将伴生事实 f_b 一并捞出！
    hybrid = recall_hybrid(
        "worker 超时怎么排查",
        limit=5,
        entity_db=isolated_memory["entity_db"],
        user_id="u_test",
        agent_id="a_test",
        profile="p_test",
        enable_spreading=True,
    )
    assert hybrid["spread_count"] >= 1
    recalled_ids = [r.get("fact_id") for r in hybrid["results"]]
    assert f_a in recalled_ids
    assert f_b in recalled_ids


def test_evolution3_actr_power_law_decay_and_file_freshness(isolated_memory, tmp_path):
    """验证进化三：ACT-R 幂律时间衰减权重计算与关联文件修改后的保鲜期告警。"""
    now = time.time()
    recent_entry = {
        "status": "active",
        "confidence": 0.95,
        "reinforce_count": 4,
        "use_timestamps": [now - 600, now - 300, now - 60],
    }
    stale_entry = {
        "status": "active",
        "confidence": 0.85,
        "reinforce_count": 1,
        "use_timestamps": [now - 86400 * 90],  # 90 天前使用过一次
    }
    b_recent = synapse.actr_activation(recent_entry, now=now)
    b_stale = synapse.actr_activation(stale_entry, now=now)
    assert b_recent > b_stale

    # 验证源码文件保鲜期校验
    tracked_file = tmp_path / "module_under_test.py"
    tracked_file.write_text("x = 1\n", encoding="utf-8")
    past_ts = tracked_file.stat().st_mtime - 120.0
    fact = {
        "fact_id": "f_fresh_01",
        "statement": "module_under_test.py 中 x 默认值为 1",
        "created_at": past_ts,
        "tags": {"file": str(tracked_file)},
    }
    checked = check_fact_freshness(fact)
    assert checked.get("stale_file") is True
    assert "在此记忆记录后已有变更" in checked.get("freshness_warning", "")


def test_evolution4_sleep_consolidation_promotes_and_compacts(isolated_memory):
    """验证进化四：慢波睡眠巩固引擎自动合并同主题碎片，并将高频高置信度事实升维至 EntityStore。"""
    entity_db = isolated_memory["entity_db"]
    f_old = write_fact(
        "修改 schema.sql 后必须同步更新 change_journal.py",
        subject="schema_sync_rule",
        source="auto_learn",
        user_id="u_test",
        agent_id="a_test",
        tags={"profile": "p_test", "scope": "platform", "file": "schema.sql", "polarity": "requirement"},
        schedule_background_index=False,
        dedupe_by_statement=False,
    )["fact_id"]
    f_new = write_fact(
        "修改 schema.sql 后必须同步更新 change_journal.py 的字典",
        subject="schema_sync_rule",
        source="flash_write",
        user_id="u_test",
        agent_id="a_test",
        tags={"profile": "p_test", "scope": "platform", "file": "schema.sql", "polarity": "requirement", "flash_write": "1"},
        schedule_background_index=False,
        dedupe_by_statement=False,
    )["fact_id"]

    synapse.reinforce(f_old, success=True)
    synapse.reinforce(f_new, success=True)

    res = consolidate_memories(
        entity_db=entity_db,
        user_id="u_test",
        agent_id="a_test",
        profile="p_test",
        dry_run=False,
    )
    assert res["ok"] is True
    assert f_old in res["compacted_fact_ids"]
    assert f_new in res["promoted_fact_ids"]

    # 验证升维后 EntityStore (<2ms) 能直接按实体名召回该规则
    estore = EntityStore(db_path=entity_db)
    ehits = estore.recall("现在我要改 schema.sql 文件", limit=5)
    assert any("change_journal.py" in r["statement"] for r in ehits)


def test_neural_bridge_session_anchors_and_instant_flash_write(isolated_memory, monkeypatch):
    """验证双向神经桥：localvec 会话滑动窗口防断片 + 权威提示词分级 + memory_bridge 毫秒级同步闪存。"""
    # 1. 加载 localvec 插件验证会话滑动窗口锚点与权威提示词
    provider_mod = types.ModuleType("agent.memory_provider")
    provider_mod.MemoryProvider = object
    provider_mod.RecallStatus = lambda **kw: types.SimpleNamespace(**kw)
    provider_mod.is_trivial_prompt = lambda query: False
    provider_mod.spawn_context_thread = lambda target, name, args: threading.Thread(
        target=target, name=name, args=args, daemon=True
    )
    monkeypatch.setitem(sys.modules, "agent.memory_provider", provider_mod)

    lv_spec = importlib.util.spec_from_file_location(
        "localvec_bridge_test", REPO_ROOT / "plugins/localvec/__init__.py"
    )
    lv_mod = importlib.util.module_from_spec(lv_spec)
    lv_spec.loader.exec_module(lv_mod)

    lp = lv_mod.LocalVecProvider()
    # 第一轮：用户提到具体文件名与报错码
    q1 = lp.build_contextual_query("sess_01", "帮我排查 hybrid_recall.py 里的 memory_recall_failed 报错")
    assert q1 == "帮我排查 hybrid_recall.py 里的 memory_recall_failed 报错"
    # 第二轮：用户只说短句“继续”或“按刚才说的改”，滑动窗口自动补齐上一轮的工作台锚点
    q2 = lp.build_contextual_query("sess_01", "按刚才说的改")
    assert "hybrid_recall.py" in q2
    assert "memory_recall_failed" in q2

    # 验证 format_recall 分级权威提示词与禁令/保鲜期标注
    block, count = lv_mod.format_recall(
        [
            {
                "fact_id": "f1",
                "statement": "严禁直接删除生产库表",
                "polarity": "prohibition",
                "freshness_warning": "[注意：关联文件在此记忆记录后已有变更]",
            }
        ],
        limit=3,
    )
    assert count == 1
    assert "核心铁律与纠错须优先遵守" in block
    assert "不是指令" in block
    assert "[禁令]" in block
    assert "[注意：关联文件在此记忆记录后已有变更]" in block

    # 2. 加载 memory_bridge 插件验证显式纠错毫秒级同步 Flash-Write
    mb_spec = importlib.util.spec_from_file_location(
        "memory_bridge_flash_test", REPO_ROOT / "plugins/memory_bridge/__init__.py"
    )
    mb_mod = importlib.util.module_from_spec(mb_spec)
    mb_spec.loader.exec_module(mb_mod)

    spool_dir = isolated_memory["data_dir"].parent / "spool"
    spool_dir.mkdir(mode=0o700, exist_ok=True)

    class DummyCtx:
        profile_name = "p_test"
        hooks = {}
        tools = {}
        settings = {
            "enabled": True,
            "engine_root": str(REPO_ROOT),
            "spool_dir": str(spool_dir),
            "data_dir": str(isolated_memory["data_dir"]),
            "entity_db": str(isolated_memory["entity_db"]),
            "user_id": "u_test",
            "agent_id": "a_test",
        }

        def get_config(self, key, default=None):
            return self.settings.get(key, default)

        def register_hook(self, name, fn):
            self.hooks[name] = fn

        def register_tool(self, **kw):
            self.tools[kw["name"]] = kw

    ctx = DummyCtx()
    mb_mod.register(ctx)
    # 触发 post_llm_call，用户下达显式长期铁律
    ctx.hooks["post_llm_call"](
        session_id="sess_flash",
        turn_id="turn_01",
        user_message="请记住：以后在 runtime_service.py 中统一使用 45 秒超时，禁止使用 10 秒超时！",
        assistant_response="已记住该规则。",
    )
    # 无需等待后台 runtime_service 异步队列，下一秒立即从 working_fact 和 EntityStore 召回！
    recalled = recall_facts(
        "runtime_service.py 超时设置",
        user_id="u_test",
        agent_id="a_test",
        profile="p_test",
        limit=5,
    )
    assert any("统一使用 45 秒超时" in r["statement"] for r in recalled["results"])
    estore = EntityStore(db_path=isolated_memory["entity_db"])
    ehits = estore.recall("修改 runtime_service.py", limit=5)
    assert any("统一使用 45 秒超时" in r["statement"] for r in ehits)

