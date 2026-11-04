"""Unit tests for agentcore.plan_gate."""
from __future__ import annotations

import pytest

from agentcore.plan_gate import (
    ComplexityAssessment,
    PlanGate,
    PlanPhase,
    TaskPlan,
    assess_task_complexity,
    render_plan_markdown,
    validate_plan,
)


def test_assess_task_complexity_simple():
    assessment = assess_task_complexity("帮我查一下当前目录有哪些文件")
    assert not assessment.is_complex
    assert not assessment.plan_first_required
    assert assessment.score < 3

    # Non-string safe handling
    assert not assess_task_complexity(None).is_complex  # type: ignore
    assert not assess_task_complexity({"query": "test"}).is_complex  # type: ignore
    assert not assess_task_complexity(123).is_complex  # type: ignore


def test_assess_task_complexity_complex_cases():
    assessment1 = assess_task_complexity("升级重构当前记忆系统，并在灌灌工控机上进行验证")
    assert assessment1.is_complex
    assert assessment1.plan_first_required
    assert assessment1.score >= 3
    assert any("重构" in r for r in assessment1.reasons)
    assert any("多节点" in r or "物理机" in r for r in assessment1.reasons)

    assessment2 = assess_task_complexity("全流程流水线改造，重写调度队列并跑全量回归测试")
    assert assessment2.is_complex
    assert assessment2.plan_first_required


def test_validate_plan_valid():
    plan = TaskPlan(
        task_id="task-upgrade-01",
        goal="完成记忆系统轻量流式改造",
        scope=["agentcore/memory", "tools/recall_entity.py"],
        phases=[
            PlanPhase(
                phase_id="p1",
                name="底层检索库构建",
                actions=["建立 SQLite FTS5 实体表", "写入 SEED 事实"],
                verification_criteria=["pytest tests/test_entity_recall.py 全部通过"],
            ),
        ],
        acceptance_criteria=["verify.sh 退出码为 0", "子进程耗时实测 < 30ms"],
        contingency_plan="若 FTS5 异常，平滑降级至 LIKE 子串检索",
    )
    ok, errors = validate_plan(plan)
    assert ok is True
    assert errors == []


def test_validate_plan_invalid():
    invalid_plan = {
        "task_id": "",
        "goal": "",
        "scope": [],
        "phases": [],
        "acceptance_criteria": [],
        "contingency_plan": "",
    }
    ok, errors = validate_plan(invalid_plan)
    assert ok is False
    assert len(errors) >= 5


def test_render_plan_markdown_and_zero_emoji():
    plan = TaskPlan(
        task_id="task-002",
        goal="部署独立验证门禁",
        scope=["agentcore/verification_gate.py"],
        phases=[
            PlanPhase(
                phase_id="p1",
                name="单元测试构建",
                actions=["编写 test_verification_gate.py"],
                verification_criteria=["pytest 退出码 0"],
            )
        ],
        acceptance_criteria=["测试覆盖率 100%"],
        contingency_plan="保留上一版备份",
        status="approved",
    )
    md = render_plan_markdown(plan)
    assert "### [PLAN]" in md
    assert "[APPROVED]" in md
    assert "[CRITERIA]" in md
    assert "[VERIFY]" in md
    # Strict Zero Emoji verification
    for ch in md:
        assert ord(ch) < 0x1F300 or ord(ch) > 0x1FAD6


def test_plan_gate_lifecycle():
    gate = PlanGate()
    plan_dict = {
        "task_id": "task-life-01",
        "goal": "重构网关",
        "scope": ["gateway/"],
        "phases": [
            {
                "phase_id": "p1",
                "name": "接口定义",
                "actions": ["定义抽象类"],
                "verification_criteria": ["mypy 通过"],
                "estimated_minutes": 10,
            }
        ],
        "acceptance_criteria": ["自动化测试全部通过"],
        "contingency_plan": "回退 git 版本",
    }
    task_plan = gate.register_plan(plan_dict)
    assert task_plan.status == "draft"

    approved = gate.approve_plan("task-life-01")
    assert approved.status == "approved"
    assert gate.get_plan("task-life-01") is approved
