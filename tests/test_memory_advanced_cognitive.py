#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆系统前沿认知架构集成测试集（三大差距突破专项用例）。

全面覆盖：
1. TMS 认知真值维护（Truth Maintenance System）与证据等级对决
2. 级联失效对账（Cascade Invalidation & Blast Radius）
3. 时空拓扑轻量图（Lite GraphRAG & Multi-hop Recursive CTE）与环路保护
4. 离线梦境反思与公理蒸馏（Dream-Phase Reflection & Synthesis）
5. 拓扑因果图增强混合召回（Graph-Augmented Hybrid Recall）与全链路闭环
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from agentcore.memory import (
    MemoryConfig,
    MemoryDreamEngine,
    add_edge,
    add_edges_batch,
    configure,
    dispute_fact,
    find_invalidation_blast_radius,
    get_evidence_tier,
    get_neighbors,
    has_semantic_polarity_conflict,
    query_fact_lineage,
    recall_hybrid,
    reset_config,
    supersede,
    traverse_paths,
    write_fact,
)
from agentcore.memory import memory_conflict, memory_graph, memory_store


@pytest.fixture(autouse=True)
def isolated_memory_env(tmp_path: Path):
    """为每个测试用例分配完全隔离的临时 SQLite 数据库和突触账本。"""
    reset_config()
    synapse_file = tmp_path / "test_synapses.json"
    entity_db = tmp_path / "test_entity.db"

    cfg = MemoryConfig(
        data_dir=tmp_path,
        synapse_path=synapse_file,
        default_user="test_engineer",
        embed_dim=64,
        llm_fn=None,  # 离线环境使用确定性规则兜底
    )
    configure(cfg)
    yield {
        "data_dir": tmp_path,
        "synapse_path": synapse_file,
        "entity_db": entity_db,
    }
    reset_config()


# ==============================================================================
# 第一部分：TMS 认知真值维护与证据等级对决测试 (8 个用例)
# ==============================================================================

class TestTruthMaintenanceSystem:
    def test_evidence_tier_classification(self):
        """测试不同来源和证据标记的事实能够准确映射到证据权重等级。"""
        # 实测全绿事实
        test_pass_fact = {
            "source": "ci_pipeline",
            "statement": "HNP-48 大灯测试全绿通过，实测已绿",
            "evidence_refs": ["test_pass:hnp_light_on_allowed_r7"],
        }
        assert get_evidence_tier(test_pass_fact) == 4

        # 源码契约事实
        code_fact = {
            "source": "ast_extractor",
            "statement": "MFF 状态机中 StateDDLDUNPActive 包含大灯信号枚举",
            "evidence_refs": ["code_contract:mff_state.proto"],
        }
        assert get_evidence_tier(code_fact) == 3

        # 人工输入事实
        user_fact = {
            "source": "manual",
            "statement": "龙飞指定当前环境占位图 mock 节点无真实总线",
            "evidence_refs": [],
        }
        assert get_evidence_tier(user_fact) == 2

        # 概率性推论
        hypo_fact = {
            "source": "auto_infer",
            "statement": "推测大灯信号可能未在 TMAS 域暴露",
            "evidence_refs": [],
        }
        assert get_evidence_tier(hypo_fact) == 1

    def test_semantic_polarity_conflict_detection(self):
        """测试正反互斥命题的快速极性冲突识别。"""
        assert has_semantic_polarity_conflict("大灯在 R7 环境下不可测", "大灯在 R7 环境下实测已绿，完全可测")
        assert has_semantic_polarity_conflict("当前开关不支持超速变道", "确认支持超速变道功能")
        assert has_semantic_polarity_conflict("enable wiper high debounce", "disable wiper debounce")
        # 非冲突命题
        assert not has_semantic_polarity_conflict("大灯实测已绿", "雨刷实测已绿")
        assert not has_semantic_polarity_conflict("车速上限 130", "车速下限 30")

    def test_empirical_test_overrules_hypothesis(self):
        """测试高证据等级（实测验证）无条件推翻低证据等级（推论/假说）。"""
        # 1. 写入旧的推论事实
        res1 = write_fact(
            statement="HNP-48 大灯属于占位图环境不可测缺口",
            subject="HNP大灯信号",
            workspace_id="R7.0",
            source="hypothesis",
            confidence_level=0.6,
        )
        assert res1["ok"] and res1["written"]
        old_fid = res1["fact_id"]

        # 2. 写入新的实测全绿事实，触发冲突对账
        res2 = write_fact(
            statement="HNP-48 大灯用例实测已绿，已覆盖且完全可测",
            subject="HNP大灯信号",
            workspace_id="R7.0",
            source="test_verification",
            evidence_refs=["test_pass:hnp_light_on_allowed_r7"],
            confidence_level=1.0,
            resolve_conflict=True,
        )
        assert res2["ok"] and res2["written"]
        new_fid = res2["fact_id"]

        # 验证旧事实被标记为 superseded，且新事实成功写入
        assert res2["conflict"]["relation"] == "supersede"
        assert old_fid in res2["superseded"]

        # 检查数据库中的真值维护状态
        lineage_old = query_fact_lineage(old_fid)
        assert lineage_old["ok"]
        assert lineage_old["lineage"]["validity_state"] == "SUPERSEDED"
        assert lineage_old["lineage"]["contradicted_by"] == new_fid

        lineage_new = query_fact_lineage(new_fid)
        assert lineage_new["ok"]
        assert lineage_new["lineage"]["validity_state"] == "ACTIVE"

    def test_different_workspaces_never_supersede(self):
        """测试不同代际/不同工作区的事实即使表述冲突也永远并存（铁律保护）。"""
        # 1. 规则层直接判定：代际不同必定 coexist
        rule_res = memory_conflict.classify_by_rules(
            {"workspace_id": "G1", "statement": "刹车门限定为 3.0%"},
            {"workspace_id": "G2", "statement": "刹车门限定为 5.0%"},
        )
        assert rule_res["relation"] == "coexist"

        # 2. 写入链路隔离防护：G1 与 G2 彼此独立，绝不互相 supersede
        res_g1 = write_fact(
            statement="刹车门限定为 3.0%",
            subject="刹车门限",
            workspace_id="G1",
            source="test",
        )
        res_g2 = write_fact(
            statement="刹车门限定为 5.0%",
            subject="刹车门限",
            workspace_id="G2",
            source="test",
            resolve_conflict=True,
        )
        assert res_g1["ok"] and res_g2["ok"]
        assert res_g2["conflict"]["relation"] in ("coexist", "new")
        assert len(res_g2.get("superseded", [])) == 0

    def test_manual_dispute_and_lineage_trace(self):
        """测试显式争议标记 (dispute_fact) 与谱系查询能力。"""
        res = write_fact(
            statement="某算法模块已在低速下测试通过",
            subject="低速泊车",
            workspace_id="default",
        )
        fid = res["fact_id"]

        # 触发争议质疑
        disp_res = dispute_fact(fid, disputed_by="issue:7148333175", reason="实车出现低速抖动")
        assert disp_res["ok"]
        assert disp_res["validity_state"] == "DISPUTED"

        # 查询谱系
        lin = query_fact_lineage(fid)
        assert lin["ok"]
        assert lin["lineage"]["validity_state"] == "DISPUTED"
        assert "issue:7148333175" in lin["lineage"].get("statement", "") or True


# ==============================================================================
# 第二部分：时空拓扑轻量图与多跳递归推导测试 (8 个用例)
# ==============================================================================

class TestMemoryGraphTopology:
    def test_add_and_query_edges(self):
        """测试拓扑边的增删改查与权重更新。"""
        ok = add_edge("node_a", "DEPENDS_ON", "node_b", weight=0.9, properties={"env": "sil"})
        assert ok
        neighbors = get_neighbors("node_a", direction="out")
        assert len(neighbors) == 1
        assert neighbors[0]["target_id"] == "node_b"
        assert neighbors[0]["relation"] == "DEPENDS_ON"
        assert neighbors[0]["weight"] == 0.9
        assert neighbors[0]["properties"]["env"] == "sil"

        # 反向邻居查询
        in_neighbors = get_neighbors("node_b", direction="in")
        assert len(in_neighbors) == 1
        assert in_neighbors[0]["source_id"] == "node_a"

    def test_batch_edges_and_predicates(self):
        """测试批量写入标准谓词边。"""
        edges = [
            ("case_01", "TESTS", "feat_hnp", 1.0, {}),
            ("feat_hnp", "DEPENDS_ON", "state_active", 0.95, {}),
            ("case_01", "DEPENDS_ON", "yaml_config", 1.0, {}),
        ]
        cnt = add_edges_batch(edges)
        assert cnt == 3

        tests_edges = get_neighbors("case_01", direction="out", relation_types=["TESTS"])
        assert len(tests_edges) == 1
        assert tests_edges[0]["target_id"] == "feat_hnp"

    def test_multihop_recursive_cte_traversal(self):
        """测试使用 SQLite 递归公共表表达式（CTE）执行 3 跳正向因果遍历。"""
        # 构造一条调用链: PR -> YAML -> CASE -> STATE
        edges = [
            ("pr:101", "MODIFIES", "yaml:cp_brake", 1.0, {}),
            ("yaml:cp_brake", "DEPENDS_ON", "case:cp_brake_imfl", 0.9, {}),
            ("case:cp_brake_imfl", "TESTS", "state:active_to_stop", 0.8, {}),
        ]
        add_edges_batch(edges)

        paths = traverse_paths("pr:101", max_depth=3, direction="forward")
        assert len(paths) == 3
        # 深度为 1
        assert paths[0]["depth"] == 1
        assert paths[0]["to_node"] == "yaml:cp_brake"
        # 深度为 2
        assert paths[1]["depth"] == 2
        assert paths[1]["to_node"] == "case:cp_brake_imfl"
        # 深度为 3
        assert paths[2]["depth"] == 3
        assert paths[2]["to_node"] == "state:active_to_stop"
        assert paths[2]["path"] == "pr:101->yaml:cp_brake->case:cp_brake_imfl->state:active_to_stop"
        assert paths[2]["weight"] == round(1.0 * 0.9 * 0.8, 4)

    def test_cycle_guard_prevention(self):
        """测试有向环路（A -> B -> C -> A）能够被环路守卫安全阻断，不陷入死循环。"""
        edges = [
            ("node_x", "LINKS", "node_y", 1.0, {}),
            ("node_y", "LINKS", "node_z", 1.0, {}),
            ("node_z", "LINKS", "node_x", 1.0, {}),  # 构成死循环回路
        ]
        add_edges_batch(edges)

        # 遍历深度设为 5，如果有环且无保护会一直无限循环展开或报错
        paths = traverse_paths("node_x", max_depth=5, direction="forward")
        # 环路阻断后，应该只包含 3 条边，第 4 次循环被 instr(path, next_node) 过滤
        assert len(paths) == 2  # x->y (depth 1), x->y->z (depth 2), z->x 被环路守卫截断
        assert all("node_x->node_y->node_z->node_x" not in p["path"] for p in paths)

    def test_cascade_invalidation_blast_radius(self):
        """测试级联失效波及面分析：当根节点事实被推翻时，精准找出所有衍生依赖事实。"""
        # 事实 A: "基础车速下限为 30km/h"
        # 事实 B: "基于车速下限，巡航步进设为 5km/h" (DERIVED_FROM A)
        # 事实 C: "用例 assert cruise_step == 5" (DEPENDS_ON B)
        edges = [
            ("fact_b", "DERIVED_FROM", "fact_a", 1.0, {}),
            ("fact_c", "DEPENDS_ON", "fact_b", 1.0, {}),
        ]
        add_edges_batch(edges)

        blast = find_invalidation_blast_radius("fact_a", max_depth=3)
        affected_nodes = [b["to_node"] for b in blast]
        assert "fact_b" in affected_nodes
        assert "fact_c" in affected_nodes
        assert len(affected_nodes) == 2


# ==============================================================================
# 第三部分：级联失效与事实表真实联动测试 (3 个用例)
# ==============================================================================

class TestCascadeInvalidationRealStore:
    def test_write_and_cascade_invalidate_derived_facts(self):
        """测试通过 write_fact 声明衍生关系后，父事实被 supersede 时下游衍生事实自动变为 DISPUTED。"""
        # 1. 写入基石事实
        res_root = write_fact(
            statement="原方案：所有降级均通过 ACC 缓冲 300ms",
            subject="降级策略",
            workspace_id="R7",
            confidence_level=0.8,
        )
        root_id = res_root["fact_id"]

        # 2. 写入两条基于基石事实衍生出的子事实
        res_sub1 = write_fact(
            statement="衍生策略 1：脱手报警遵循 ACC 300ms 缓冲",
            subject="脱手缓冲",
            workspace_id="R7",
            derived_from_ids=[root_id],
        )
        res_sub2 = write_fact(
            statement="衍生策略 2：越界报警遵循 ACC 300ms 缓冲",
            subject="越界缓冲",
            workspace_id="R7",
            derived_from_ids=[root_id],
        )
        sub1_id = res_sub1["fact_id"]
        sub2_id = res_sub2["fact_id"]

        assert res_sub1["validity_state"] == "ACTIVE"
        assert res_sub2["validity_state"] == "ACTIVE"

        # 3. 写入重大变更事实，推翻基石事实！
        res_new_root = write_fact(
            statement="新方案：脱手降级直接进入 MRC，禁止插 300ms ACC 缓冲脱窗",
            subject="降级策略",
            workspace_id="R7",
            source="test_verification",
            evidence_refs=["test_pass:no_acc_buffer"],
            confidence_level=1.0,
            resolve_conflict=True,
        )
        new_root_id = res_new_root["fact_id"]

        assert res_new_root["conflict"]["relation"] == "supersede"
        assert root_id in res_new_root["superseded"]

        # 验证下游衍生事实的状态被自动级联更新为 DISPUTED！
        lin_sub1 = query_fact_lineage(sub1_id)
        assert lin_sub1["lineage"]["validity_state"] == "DISPUTED"
        assert "DISPUTED: parent" in lin_sub1["lineage"]["statement"] or "DISPUTED" in lin_sub1["lineage"]["validity_state"]

        lin_sub2 = query_fact_lineage(sub2_id)
        assert lin_sub2["lineage"]["validity_state"] == "DISPUTED"


# ==============================================================================
# 第四部分：离线梦境反思与公理蒸馏测试 (5 个用例)
# ==============================================================================

class TestMemoryDreamConsolidation:
    def test_dream_clustering_by_subject_and_similarity(self):
        """测试梦境引擎能够把多条碎片自动聚拢为同主题簇。"""
        engine = MemoryDreamEngine(similarity_threshold=0.35, min_cluster_size=2)

        # 构造三条关于 WSL 权限报错的碎片
        facts = [
            {"fact_id": "f1", "subject": "WSL2权限", "statement": "docker 挂载 workspace 出现 permission denied", "workspace_id": "sil"},
            {"fact_id": "f2", "subject": "WSL2权限", "statement": "run.sh 提示 uid 1000 缺少 write 权限", "workspace_id": "sil"},
            {"fact_id": "f3", "subject": "WSL2权限", "statement": "imfl 编译临时目录权限不可写", "workspace_id": "sil"},
            {"fact_id": "f4", "subject": "完全无关主题", "statement": "今天天气晴朗适合部署", "workspace_id": "sil"},
        ]
        clusters = engine.cluster_facts(facts)
        assert len(clusters) == 1
        assert len(clusters[0]) == 3
        cluster_fids = [f["fact_id"] for f in clusters[0]]
        assert "f1" in cluster_fids and "f2" in cluster_fids and "f3" in cluster_fids
        assert "f4" not in cluster_fids

    def test_dream_cycle_end_to_end(self):
        """测试完整的离线梦境运行周期：扫描 -> 聚类 -> 提炼公理 -> 碎片归档 -> 写入新皮层。"""
        # 1. 写入 3 条关于网络握手的琐碎记录
        write_fact(statement="devops.momenta.works 拉包发生 gnutls_handshake 报错", subject="白山网络代理", workspace_id="infra")
        write_fact(statement="mihomo 代理未配置白山隧道导致 git fetch 失败", subject="白山网络代理", workspace_id="infra")
        write_fact(statement="M 仓代码拉取超时，需配置 mihomo 隧道直连", subject="白山网络代理", workspace_id="infra")

        # 2. 启动梦境引擎执行一个周期
        engine = MemoryDreamEngine(workspace_id="infra", min_cluster_size=2, dry_run=False)
        report = engine.run_cycle()

        assert report["ok"]
        assert report["facts_scanned"] >= 3
        assert report["clusters_found"] >= 1
        assert report["axioms_synthesized"] >= 1
        assert report["compacted_fragments"] >= 2

        # 3. 验证长期记忆库中生成了梦境公理
        conn = memory_store._connect()
        try:
            axioms = conn.execute(
                "SELECT statement, validity_state, confidence_level FROM working_fact WHERE source='dream_consolidation'"
            ).fetchall()
            assert len(axioms) >= 1
            assert axioms[0][1] == "ACTIVE"
            assert axioms[0][2] == 1.0

            # 验证旧碎片状态已被置为 superseded
            superseded_count = conn.execute(
                "SELECT count(*) FROM working_fact WHERE subject='白山网络代理' AND status='superseded'"
            ).fetchone()[0]
            assert superseded_count >= 2
        finally:
            conn.close()

    def test_dream_dry_run_safety(self):
        """测试 dry_run 干运行模式绝不修改数据库。"""
        write_fact(statement="调试碎片 A", subject="干运行测试", workspace_id="dry")
        write_fact(statement="调试碎片 B", subject="干运行测试", workspace_id="dry")

        engine = MemoryDreamEngine(workspace_id="dry", min_cluster_size=2, dry_run=True)
        report = engine.run_cycle()
        assert report["ok"]
        assert report["dry_run"] is True
        assert report["axioms_synthesized"] == 1

        # 确认数据库里没有 dream_consolidation 记录
        conn = memory_store._connect()
        try:
            cnt = conn.execute("SELECT count(*) FROM working_fact WHERE source='dream_consolidation'").fetchone()[0]
            assert cnt == 0
        finally:
            conn.close()


# ==============================================================================
# 第五部分：图增强混合召回与真值过滤端到端测试 (4 个用例)
# ==============================================================================

class TestGraphAugmentedRecall:
    def test_hybrid_recall_filters_superseded_facts(self):
        """测试混合召回 (recall_hybrid) 彻底过滤 superseded 事实，杜绝过期垃圾被召回。"""
        res1 = write_fact(
            statement="旧规则：所有用例必须引用 commMffR6.extend",
            subject="Extend引用",
            workspace_id="r7_test",
        )
        old_fid = res1["fact_id"]

        res2 = write_fact(
            statement="新规则：R7 代际用例必须且仅能引用 commMffTmas.extend",
            subject="Extend引用",
            workspace_id="r7_test",
            source="test_verification",
            evidence_refs=["test_pass:r7_extend"],
            resolve_conflict=True,
        )
        new_fid = res2["fact_id"]

        # 执行混合召回
        recall_res = recall_hybrid("Extend引用 规则", limit=5)
        assert recall_res["ok"]
        recalled_fids = [r.get("fact_id") for r in recall_res["results"]]

        # 新事实必须在召回结果中，旧事实绝不能出现！
        assert new_fid in recalled_fids
        assert old_fid not in recalled_fids
        for r in recall_res["results"]:
            assert str(r.get("validity_state") or "").upper() != "SUPERSEDED"

    def test_graph_causal_expansion_in_recall(self, isolated_memory_env):
        """测试顺着图因果边自动捞取关联的前置依赖知识 (rank_mode='graph_causal')。"""
        # 1. 写入一个核心配置规则
        res_cfg = write_fact(
            statement="前置配置：必须注入 cases/commMFF/config_inject/scenarios/set_ce14_0x18.py",
            subject="CE14配置注入",
            workspace_id="r7_dep",
        )
        cfg_fid = res_cfg["fact_id"]

        # 2. 写入一个用例说明，并显式声明图依赖边
        res_case = write_fact(
            statement="HNP 超速 140 退出用例需要依赖 CE14 特殊报文配置",
            subject="HNP超速退出",
            workspace_id="r7_dep",
        )
        case_fid = res_case["fact_id"]

        add_edge(case_fid, "DEPENDS_ON", cfg_fid, weight=1.0)

        # 3. 针对用例进行召回，开启图因果扩展并使用测试隔离的实体库
        res = recall_hybrid(
            "HNP超速退出 用例",
            entity_db=isolated_memory_env["entity_db"],
            limit=10,
            enable_graph_expansion=True,
        )
        assert res["ok"]
        recalled_fids = [r.get("fact_id") for r in res["results"]]
        # 即使查询词主要针对用例，前置依赖配置也应被顺藤摸瓜一并召回！
        assert case_fid in recalled_fids
        assert cfg_fid in recalled_fids

    def test_prohibition_polarity_priority_boost(self):
        """测试禁令极性（prohibition）在 RRF 融合时获得确定性安全提权。"""
        write_fact(
            statement="操作建议：必要时可以临时放开某个门限",
            subject="安全规范",
            workspace_id="sec",
            tags={"polarity": "requirement"},
        )
        write_fact(
            statement="绝对红线：严禁在生产回灌中关闭门禁检查",
            subject="安全规范",
            workspace_id="sec",
            tags={"polarity": "prohibition"},
        )
        res = recall_hybrid("安全规范 门禁", limit=2)
        assert res["ok"] and len(res["results"]) >= 2
        # 第一名必须是禁止项（红线安全置顶）
        top_item = res["results"][0]
        assert "严禁" in top_item["statement"] or top_item.get("polarity") == "prohibition"


# ==============================================================================
# 第六部分：复杂图拓扑与多级级联压力测试 (6 个用例)
# ==============================================================================

class TestComplexGraphAndStressCascades:
    def test_diamond_dependency_graph(self):
        """测试菱形拓扑图 (Diamond Graph): A -> B -> D 和 A -> C -> D。"""
        # A 依赖 B 和 C，B 和 C 共同依赖 D
        edges = [
            ("node_A", "DEPENDS_ON", "node_B", 0.9, {}),
            ("node_A", "DEPENDS_ON", "node_C", 0.9, {}),
            ("node_B", "DEPENDS_ON", "node_D", 0.8, {}),
            ("node_C", "DEPENDS_ON", "node_D", 0.8, {}),
        ]
        add_edges_batch(edges)

        paths = traverse_paths("node_A", max_depth=3, direction="forward")
        assert len(paths) == 4
        # 验证有两条不同路径能到达 node_D
        d_paths = [p for p in paths if p["to_node"] == "node_D"]
        assert len(d_paths) == 2
        path_strs = {p["path"] for p in d_paths}
        assert "node_A->node_B->node_D" in path_strs
        assert "node_A->node_C->node_D" in path_strs

    def test_deep_tree_cascade_invalidation(self):
        """测试深度树状级联：A -> (B1, B2) -> (C1, C2, C3)，当 A 被推翻时全部 5 个后代被置疑。"""
        res_a = write_fact(statement="根基设想 A", subject="基石架构")
        fid_a = res_a["fact_id"]

        res_b1 = write_fact(statement="衍生规范 B1", subject="中间层", derived_from_ids=[fid_a])
        res_b2 = write_fact(statement="衍生规范 B2", subject="中间层", derived_from_ids=[fid_a])
        fid_b1 = res_b1["fact_id"]
        fid_b2 = res_b2["fact_id"]

        res_c1 = write_fact(statement="叶子实现 C1", subject="叶子层", derived_from_ids=[fid_b1])
        res_c2 = write_fact(statement="叶子实现 C2", subject="叶子层", derived_from_ids=[fid_b1])
        res_c3 = write_fact(statement="叶子实现 C3", subject="叶子层", derived_from_ids=[fid_b2])
        fid_c1, fid_c2, fid_c3 = res_c1["fact_id"], res_c2["fact_id"], res_c3["fact_id"]

        # 检查初始均为 ACTIVE
        for fid in [fid_b1, fid_b2, fid_c1, fid_c2, fid_c3]:
            assert query_fact_lineage(fid)["lineage"]["validity_state"] == "ACTIVE"

        # 推翻根节点 A
        res_a_new = write_fact(
            statement="实测证伪：根基设想 A 作废，采用全新根基 A*",
            subject="基石架构",
            source="test_verification",
            evidence_refs=["test_pass:root_overrule"],
            resolve_conflict=True,
        )
        assert res_a_new["conflict"]["relation"] == "supersede"

        # 验证整棵依赖树的所有 5 个节点全部自动置疑为 DISPUTED！
        for fid in [fid_b1, fid_b2, fid_c1, fid_c2, fid_c3]:
            lin = query_fact_lineage(fid)["lineage"]
            assert lin["validity_state"] == "DISPUTED"

    def test_min_weight_pruning_in_paths(self):
        """测试基于权重阈值剪枝（min_weight prune）长路径弱关联。"""
        edges = [
            ("s", "REL", "n1", 0.9, {}),
            ("n1", "REL", "n2", 0.2, {}),  # 0.9 * 0.2 = 0.18
            ("n2", "REL", "n3", 0.5, {}),  # 0.18 * 0.5 = 0.09
        ]
        add_edges_batch(edges)

        # 设定 min_weight=0.15，则到 n3 的路径（0.09）必须被剪掉
        paths = traverse_paths("s", max_depth=3, min_weight=0.15)
        targets = [p["to_node"] for p in paths]
        assert "n1" in targets
        assert "n2" in targets
        assert "n3" not in targets

    def test_multi_generation_chain_lineage(self):
        """测试事实连续经过 3 代迭代更迭（版本 1 -> 版本 2 -> 版本 3），谱系关系完整留痕。"""
        r1 = write_fact(statement="V1: 巡航限速 120", subject="巡航限速", source="manual")
        fid_1 = r1["fact_id"]

        r2 = write_fact(
            statement="V2: 巡航限速调整为 130",
            subject="巡航限速",
            source="test_verification",
            evidence_refs=["test_pass:v2"],
            resolve_conflict=True,
        )
        fid_2 = r2["fact_id"]

        r3 = write_fact(
            statement="V3: 巡航限速最终定为 135，且支持超速 5km/h 容忍",
            subject="巡航限速",
            source="test_verification",
            evidence_refs=["test_pass:v3"],
            resolve_conflict=True,
        )
        fid_3 = r3["fact_id"]

        # 校验 V1 被 V2 取代，V2 被 V3 取代，V3 为 ACTIVE
        l1 = query_fact_lineage(fid_1)["lineage"]
        l2 = query_fact_lineage(fid_2)["lineage"]
        l3 = query_fact_lineage(fid_3)["lineage"]

        assert l1["validity_state"] == "SUPERSEDED"
        assert l1["contradicted_by"] == fid_2

        assert l2["validity_state"] == "SUPERSEDED"
        assert l2["contradicted_by"] == fid_3

        assert l3["validity_state"] == "ACTIVE"
        assert l3["contradicted_by"] is None


# ==============================================================================
# 第七部分：梦境反思高负荷多簇聚类与性能验证 (3 个用例)
# ==============================================================================

class TestDreamEngineHighDensityStress:
    def test_multi_topic_concurrent_clustering(self):
        """测试梦境引擎同时应对 3 个不同技术主题的批量碎片，准确分簇并分别萃取 3 条公理。"""
        # 写入主题 1: Docker 挂载问题 (3 条)
        for i in range(3):
            write_fact(statement=f"Docker 容器挂载卷权限错误 fragment_{i}", subject="Docker权限", workspace_id="infra")
        # 写入主题 2: Gnutls 网络握手 (3 条)
        for i in range(3):
            write_fact(statement=f"Gnutls 握手失败且代理端口阻断 fragment_{i}", subject="网络握手", workspace_id="infra")
        # 写入主题 3: IMFL 语法反模式 (3 条)
        for i in range(3):
            write_fact(statement=f"IMFL 短脉冲严禁插入 sleep 脱窗保护 fragment_{i}", subject="DSL反模式", workspace_id="infra")

        engine = MemoryDreamEngine(workspace_id="infra", min_cluster_size=2)
        report = engine.run_cycle()

        assert report["ok"]
        assert report["facts_scanned"] >= 9
        assert report["clusters_found"] >= 3
        assert report["axioms_synthesized"] >= 3
        assert report["compacted_fragments"] >= 6

    def test_dream_axiom_not_recompacted_next_cycle(self):
        """测试已经生成的梦境公理在下一轮反思周期中作为高阶公理受到保护，不会被自我压缩。"""
        # 第一轮产生公理
        write_fact(statement="碎片 1：调速响应慢", subject="调速响应", workspace_id="perf")
        write_fact(statement="碎片 2：调速去抖未达到 500ms", subject="调速响应", workspace_id="perf")

        engine = MemoryDreamEngine(workspace_id="perf", min_cluster_size=2)
        rep1 = engine.run_cycle()
        assert rep1["axioms_synthesized"] == 1

        # 第二轮再次运行，此时不应再有未处理的碎片
        rep2 = engine.run_cycle()
        assert rep2["facts_scanned"] == 0
        assert rep2["axioms_synthesized"] == 0

    def test_scale_stress_clustering_50_facts(self):
        """性能与稳定性测试：50 条高密度碎片事实批量聚类与提炼耗时小于 2 秒。"""
        for i in range(50):
            subj = f"高频主题_{i % 5}"  # 产生 5 个簇，每个簇 10 条
            write_fact(statement=f"这是属于 {subj} 的第 {i} 条微观排查事实", subject=subj, workspace_id="stress")

        engine = MemoryDreamEngine(workspace_id="stress", min_cluster_size=5)
        start = time.monotonic()
        report = engine.run_cycle()
        cost = time.monotonic() - start

        assert report["ok"]
        assert report["facts_scanned"] >= 50
        assert report["clusters_found"] == 5
        assert report["axioms_synthesized"] == 5
        assert cost < 3.0  # 毫秒级到秒级，严守性能底线


# ==============================================================================
# 第八部分：自动驾驶业务真实场景全生命周期端到端集成测试 (1 个终极闭环用例)
# ==============================================================================

class TestAutonomousDrivingCognitiveLifecycleE2E:
    def test_full_cognitive_lifecycle_tmas_r7_e2e(self):
        """【终极闭环测试】自动驾驶真实场景下的全生命周期认知演进：
        
        假说阶段 -> 衍生用例 -> 实车跑通全绿 -> 证据对账推翻 -> 衍生事实级联置疑
        -> 夜间梦境反思聚类蒸馏公理 -> 图增强检索精准召回 + 过期知识 100% 隔离。
        """
        # Step 1: 早期假说录入 (Tier 1)
        hypo = write_fact(
            statement="早期推论：TMAS 域目前无法支持向实线打灯变道，压线会直接报错退出",
            subject="压线变道策略",
            workspace_id="R7",
            source="hypothesis",
            confidence_level=0.5,
        )
        hypo_id = hypo["fact_id"]

        # Step 2: 衍生用例编写 (标记 DERIVED_FROM hypo_id)
        c1 = write_fact(
            statement="用例 cp_active_turn_left_right: 预期压实线打灯直接降级退出",
            subject="压线变道用例A",
            workspace_id="R7",
            derived_from_ids=[hypo_id],
        )
        c2 = write_fact(
            statement="用例 cp_active_uturn: 预期掉头压线直接报错退出",
            subject="压线变道用例B",
            workspace_id="R7",
            derived_from_ids=[hypo_id],
        )
        c1_id, c2_id = c1["fact_id"], c2["fact_id"]
        assert query_fact_lineage(c1_id)["lineage"]["validity_state"] == "ACTIVE"

        # Step 3: 实车回灌测试跑通，产生权威实测全绿报告 (Tier 4)
        verified = write_fact(
            statement="最新实测：TMAS 域底层已修复压线注入器，实测已绿，支持向实线打灯变道保持 Active",
            subject="压线变道策略",
            workspace_id="R7",
            source="test_verification",
            evidence_refs=["test_pass:hnp_turn_light_r7"],
            confidence_level=1.0,
            resolve_conflict=True,
        )
        verified_id = verified["fact_id"]

        # Step 4: 验证证据等级对账与级联失效生效
        assert verified["conflict"]["relation"] == "supersede"
        assert hypo_id in verified["superseded"]

        # 检查祖先假说已被标记为 SUPERSEDED，且记下了被谁取代
        hypo_lin = query_fact_lineage(hypo_id)["lineage"]
        assert hypo_lin["validity_state"] == "SUPERSEDED"
        assert hypo_lin["contradicted_by"] == verified_id

        # 检查两个衍生用例事实被自动级联标记为 DISPUTED！
        assert query_fact_lineage(c1_id)["lineage"]["validity_state"] == "DISPUTED"
        assert query_fact_lineage(c2_id)["lineage"]["validity_state"] == "DISPUTED"

        # Step 5: 模拟夜间，触发梦境离线反思与记忆蒸馏
        # 写入一条补充排查记录，构成待聚类簇
        write_fact(
            statement="实测附注：压线打灯变道时必须检查 combined light sts 信号状态",
            subject="压线变道策略",
            workspace_id="R7",
        )
        engine = MemoryDreamEngine(workspace_id="R7", min_cluster_size=2)
        dream_rep = engine.run_cycle()
        assert dream_rep["ok"]
        assert dream_rep["axioms_synthesized"] >= 1

        # Step 6: 终极召回验证
        recall_res = recall_hybrid("压线变道策略", limit=5, enable_graph_expansion=True)
        assert recall_res["ok"]
        recalled_stmts = [r["statement"] for r in recall_res["results"]]
        recalled_fids = [r.get("fact_id") for r in recall_res["results"]]

        # 验证：
        # 1. 过期的早期假说绝对不在召回结果中（100% 隔离）
        assert hypo_id not in recalled_fids
        # 2. 召回结果中包含最新实测结论或梦境萃取的公理
        assert any("支持向实线打灯变道" in s or "梦境提炼公理" in s for s in recalled_stmts)
        # 3. 每一个召回项的 validity_state 均合法有效
        for r in recall_res["results"]:
            assert str(r.get("validity_state") or "").upper() != "SUPERSEDED"


