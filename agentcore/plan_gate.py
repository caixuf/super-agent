"""agentcore.plan_gate — 显式规划先行协议 (Plan-First Protocol).

终结盲目 ReAct 试错循环：
1. 任务复杂度自检：多步骤、跨主机、代码重构、破坏性运维任务强制先行推演；
2. 结构化任务执行蓝图 Schema (Goal, Scope, Phases, Acceptance Criteria, Rollback)；
3. 计划不可变审计与格式校验；
4. 渲染保真纯文本与 Markdown 执行清单（符合零 Emoji 规范）。
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

logger = logging.getLogger(__name__)


@dataclass
class PlanPhase:
    phase_id: str
    name: str
    actions: List[str]
    verification_criteria: List[str]
    estimated_minutes: int = 5


@dataclass
class TaskPlan:
    task_id: str
    goal: str
    scope: List[str]
    phases: List[PlanPhase]
    acceptance_criteria: List[str]
    contingency_plan: str
    created_at: float = field(default_factory=time.time)
    status: str = "draft"  # draft, approved, executing, completed, failed

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "scope": list(self.scope),
            "phases": [
                {
                    "phase_id": p.phase_id,
                    "name": p.name,
                    "actions": list(p.actions),
                    "verification_criteria": list(p.verification_criteria),
                    "estimated_minutes": p.estimated_minutes,
                }
                for p in self.phases
            ],
            "acceptance_criteria": list(self.acceptance_criteria),
            "contingency_plan": self.contingency_plan,
            "created_at": self.created_at,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TaskPlan:
        phases = [
            PlanPhase(
                phase_id=p["phase_id"],
                name=p["name"],
                actions=p.get("actions", []),
                verification_criteria=p.get("verification_criteria", []),
                estimated_minutes=p.get("estimated_minutes", 5),
            )
            for p in data.get("phases", [])
        ]
        return cls(
            task_id=data["task_id"],
            goal=data["goal"],
            scope=data.get("scope", []),
            phases=phases,
            acceptance_criteria=data.get("acceptance_criteria", []),
            contingency_plan=data.get("contingency_plan", ""),
            created_at=data.get("created_at", time.time()),
            status=data.get("status", "draft"),
        )


@dataclass
class ComplexityAssessment:
    is_complex: bool
    score: int
    reasons: List[str]
    plan_first_required: bool


COMPLEXITY_PATTERNS = [
    (r"(重构|重写|迁移|升级|改造)", 3, "涉及系统代码重构或架构升级"),
    (r"(多机|跨机|灌灌|远程机|跳板|工控机)", 3, "涉及物理机或多节点协同操作"),
    (r"(内核|驱动|掉卡|systemd|重启|daemon)", 3, "涉及系统底层服务或硬件状态变更"),
    (r"(步骤|第一步|阶段|全流程|流水线|pipeline)", 2, "具有显式多步骤执行特征"),
    (r"(测试集|全量回归|verify\.sh|基准测试|eval)", 2, "涉及大规模门禁回归验证"),
    (r"(rm -rf|覆盖|强推|git push -f|清空)", 4, "具有不可逆或高危物理影响"),
]


def assess_task_complexity(prompt: str) -> ComplexityAssessment:
    """评估输入任务的系统复杂度，判定是否必须强制输出执行蓝图 (Plan-First)"""
    if not isinstance(prompt, str) or not prompt.strip():
        return ComplexityAssessment(is_complex=False, score=0, reasons=[], plan_first_required=False)

    score = 0
    reasons = []

    for pattern, weight, reason in COMPLEXITY_PATTERNS:
        if re.search(pattern, prompt, re.IGNORECASE):
            score += weight
            reasons.append(reason)

    # 复杂任务判定门限：得分 >= 3 分强制 Plan-First
    plan_required = score >= 3
    return ComplexityAssessment(
        is_complex=score >= 3,
        score=score,
        reasons=reasons,
        plan_first_required=plan_required,
    )


def validate_plan(plan: Union[TaskPlan, Dict[str, Any]]) -> Tuple[bool, List[str]]:
    """严格校验任务执行计划是否完整合法，杜绝空洞草案。"""
    data = plan.to_dict() if isinstance(plan, TaskPlan) else plan
    errors: List[str] = []

    if not data.get("task_id", "").strip():
        errors.append("缺少 task_id")
    if not data.get("goal", "").strip():
        errors.append("缺少明确的执行目标 (goal)")
    if not data.get("scope"):
        errors.append("缺少任务范围边界清单 (scope)")

    phases = data.get("phases", [])
    if not phases or not isinstance(phases, list):
        errors.append("至少需要定义 1 个明确的执行阶段 (phases)")
    else:
        for i, p in enumerate(phases):
            p_name = p.get("name", "").strip()
            if not p_name:
                errors.append(f"阶段 #{i+1} 缺少阶段名称")
            if not p.get("actions"):
                errors.append(f"阶段 [{p_name or i+1}] 缺少具体行动步骤 (actions)")
            if not p.get("verification_criteria"):
                errors.append(f"阶段 [{p_name or i+1}] 缺少验证断言 (verification_criteria)")

    if not data.get("acceptance_criteria"):
        errors.append("缺少最终验收准则清单 (acceptance_criteria)")
    if not data.get("contingency_plan", "").strip():
        errors.append("缺少风险回滚与应急预案 (contingency_plan)")

    return (len(errors) == 0, errors)


def render_plan_markdown(plan: Union[TaskPlan, Dict[str, Any]]) -> str:
    """将执行计划渲染为纯文本与工程规范兼备的 Markdown 蓝图 (符合零 Emoji 规范)"""
    data = plan.to_dict() if isinstance(plan, TaskPlan) else plan
    lines: List[str] = [
        f"### [PLAN] 任务执行蓝图: {data.get('goal', '未命名任务')}",
        f"- 任务编号: {data.get('task_id')}",
        f"- 计划状态: [{data.get('status', 'draft').upper()}]",
        "- 任务范围:",
    ]
    for s in data.get("scope", []):
        lines.append(f"  * {s}")

    lines.append("\n#### 分步推进计划:")
    for idx, p in enumerate(data.get("phases", []), start=1):
        lines.append(f"{idx}. **{p.get('name', '阶段')}** (预估 {p.get('estimated_minutes', 5)} 分钟)")
        lines.append("   - 行动步骤:")
        for act in p.get("actions", []):
            lines.append(f"     + {act}")
        lines.append("   - 验收断言:")
        for cri in p.get("verification_criteria", []):
            lines.append(f"     [CRITERIA] {cri}")

    lines.append("\n#### 终态验收准则:")
    for ac in data.get("acceptance_criteria", []):
        lines.append(f"- [VERIFY] {ac}")

    lines.append(f"\n#### 风险与回滚预案:\n{data.get('contingency_plan', '无特定应急措施')}")
    return "\n".join(lines)


class PlanGate:
    """规划先行控制门，集成计划生成、校验与流转控制。"""

    def __init__(self) -> None:
        self._plans: Dict[str, TaskPlan] = {}

    def assess(self, prompt: str) -> ComplexityAssessment:
        return assess_task_complexity(prompt)

    def register_plan(self, plan: Union[TaskPlan, Dict[str, Any]]) -> TaskPlan:
        task_plan = plan if isinstance(plan, TaskPlan) else TaskPlan.from_dict(plan)
        valid, errors = validate_plan(task_plan)
        if not valid:
            raise ValueError(f"Plan-First 计划校验失败: {'; '.join(errors)}")
        self._plans[task_plan.task_id] = task_plan
        return task_plan

    def get_plan(self, task_id: str) -> Optional[TaskPlan]:
        return self._plans.get(task_id)

    def approve_plan(self, task_id: str) -> TaskPlan:
        plan = self._plans.get(task_id)
        if not plan:
            raise KeyError(f"未找到任务计划: {task_id}")
        plan.status = "approved"
        return plan
