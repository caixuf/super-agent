"""agentcore.colleague_dev — Official definitions and contracts for the Engineering Colleague Team.

Replaces the legacy 'read-only' constraint with full-fledged engineering colleague roles
equipped with coding tools (read, write, edit, bash) operating inside isolated worktrees.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import fnmatch
import importlib
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Tuple, Union
import uuid

logger = logging.getLogger(__name__)

OWNS = "owns"
APPENDS = "appends"
NEVER = "never"
ARTIFACTS = "artifacts"

HARD = "hard"
SOFT = "soft"


@dataclass(frozen=True)
class RolePathOwnership:
    """Raven-inspired path ownership contract for engineering colleague roles.

    Defines explicit filesystem boundaries for multi-agent collaboration:
    - owns: full read/write/create/delete authority
    - appends: append-only authority (deletions/overwrites forbidden)
    - never: strictly forbidden from touching these paths
    - artifacts: build, telemetry, or test outputs
    - enforce_write: 'hard' (raises PermissionError) or 'soft' (logs warning)
    - enforce_read: 'soft' or 'hard'
    """
    role: str
    owns: Tuple[str, ...] = ()
    appends: Tuple[str, ...] = ()
    never: Tuple[str, ...] = ()
    artifacts: Tuple[str, ...] = ()
    enforce_write: str = HARD
    enforce_read: str = SOFT


# Standard roster of the Universal Engineering Colleague Team
# Super Agent core provides these 8 canonical engineering roles out-of-the-box.
# Business domains can customize, extend, or override duties via register_role_override().
DEV_ROLES: Dict[str, Dict[str, Any]] = {
    "architect": {
        "display_name": "需求边界与系统架构师",
        "duty": "纯黑盒拆解自然语言需求，提取因果契约与架构规格蓝图，严格防止未经验证的实现细节污染系统边界",
        "can_do": [
            "拆解自然语言需求因果链与验收标准",
            "制定接口、数据契约与模块依赖规范",
            "产出不可变的需求规格与架构蓝图制品",
            "裁决需求冲突并维护系统演进一致性",
        ],
        "boundaries": [
            "黑盒契约原则：不得首先阅读内部实现源码来定义需求",
            "不得擅自妥协产品业务核心约束与质量底线",
        ],
    },
    "cases": {
        "display_name": "核心研发与用例开发专家",
        "duty": "根据架构规格与设计契约，在隔离工作区分支中实现功能逻辑、状态转移并编写完备用例",
        "can_do": [
            "逆向状态转移条件、时序约束与接口协议",
            "编写高标准规范的核心模块实现与用例代码",
            "配置组件运行入口、环境参数与依赖声明",
            "根据代码评审意见进行定向重构与缺陷修复",
        ],
        "boundaries": [
            "隔离施工原则：严禁在未受控主分支直接提交代码",
            "短时序高敏链路前禁止插入无谓硬延时导致脱窗死锁",
            "必须包含退出条件、异常防护与反向断言",
        ],
    },
    "reviewer": {
        "display_name": "代码审查与红队质检员",
        "duty": "严把代码与用例质量关，执行语法与架构反模式守卫、时序竞争分析与测试覆盖率核验",
        "can_do": [
            "审查反模式代码、坏味道与潜在未捕获异常",
            "核验正向、反向边界与极端场景覆盖完整性",
            "分析多线程/异步时序竞争、死锁与资源泄漏风险",
            "出具结构化缺陷扣分项与定向修改指导报告",
        ],
        "boundaries": [
            "质量门禁底线：存在反模式或致命隐患一律驳回，严禁盲目放行",
            "证据不足或未经物理验证时严禁给出通过结论",
        ],
    },
    "test": {
        "display_name": "自动化测试与仿真验证专家",
        "duty": "在隔离测试环境中驱动全量与增量测试管线，执行多级自动化门禁与真实报告归档",
        "can_do": [
            "拉起隔离容器与自动化测试驱动进程",
            "执行回归测试套件与持续集成质量门禁",
            "采集结构化执行轨迹、断言结果与日志流",
            "生成不可变 HTML/JSON 测试与度量报告",
        ],
        "boundaries": [
            "真实物理原则：测试结果以真实进程退出码与断言为准，严禁伪造测试结果",
            "构建依赖与制品 SHA256 校验不通过时必须抛错终止，严禁静默回退",
        ],
    },
    "triage": {
        "display_name": "缺陷定性与根因诊断专家",
        "duty": "对测试失败、系统崩溃与异常行为进行机制级排查，确定性归因为软件缺陷、测试错误或环境问题",
        "can_do": [
            "比对系统状态跳变轨迹、调用堆栈与异常上下文",
            "定位跨进程通信延迟、资源竞争与调度瓶颈",
            "查验历史先例、故障模式库与决策锚点",
            "构建最小复现用例并产出确凿归因报告",
        ],
        "boundaries": [
            "严禁把未经验证的主观臆测包装为确定性结论",
            "证据不足时必须如实报告无法确诊并列明缺失证据链",
        ],
    },
    "backend": {
        "display_name": "服务端与底层系统工程师",
        "duty": "开发与维护服务端调度框架、REST/SSE/RPC 接口、任务持久化与事务一致性保障",
        "can_do": [
            "编写高性能 API 路由接口与数据契约模型",
            "加固数据库原子事务与行级/文件锁并发控制",
            "优化任务调度执行器与严格串行/并发队列",
            "维护跨平台与外部服务双向可靠同步",
        ],
        "boundaries": [
            "严禁绕开锁机制拉起不受控子进程",
            "严禁绕过连接池与安全校验直接修改底层任务数据库",
        ],
    },
    "frontend": {
        "display_name": "表现层与交互排版工程师",
        "duty": "负责原生交互卡片排版、数据可视化呈现、富文本渲染优化与流式交互体验",
        "can_do": [
            "构造符合现代卡片规范的结构化交互布局",
            "优化 Markdown、数据表格与图表的可视化渲染",
            "调优文档导出、排版分页与流式反馈体验",
        ],
        "boundaries": [
            "严格遵守表现层规范，禁用过时或废弃排版标签",
            "保证单卡生命周期唯一性，禁止二次触发退役交互组件",
        ],
    },
    "ops": {
        "display_name": "平台运维与稳定性专家",
        "duty": "监控系统服务存活、资源水位、部署更新、自动化巡检与幂等运维告警",
        "can_do": [
            "巡检守护进程与系统服务健康度",
            "监控任务租约、看门狗心跳与 Socket 连接假死",
            "执行跨节点安全部署、配置校验与版本回滚",
            "向指定运维通道投递结构化带幂等键的故障告警",
        ],
        "boundaries": [
            "告警必须带 SHA256 幂等键，严禁重复骚扰外部通道",
            "严禁在代码、日志与通知中泄露凭据与敏感密钥",
        ],
    },
}

#: Raven-inspired canonical path ownership matrix for the engineering colleague team
DEV_ROLE_OWNERSHIP: Dict[str, Dict[str, Any]] = {
    "architect": {
        "owns": ("docs/**", "specs/**", "SPEC.md", "README.md", "architecture/**"),
        "appends": ("DECISIONS.md", "CHANGELOG.md", "HUMAN_DECISIONS.md", "AGENT_DECISIONS.md"),
        "never": ("tests/**", "case_set/**", "src/**", "*.py", ".git/**"),
        "artifacts": ("docs/architecture/**", "specs/**"),
        "enforce_write": HARD,
    },
    "cases": {
        "owns": ("src/**", "case_set/**", "agentcore/**", "lib/**", "*.py"),
        "appends": ("FIXLOG.md", "CHANGELOG.md"),
        "never": ("docs/specs/**", "SPEC.md", ".git/**"),
        "artifacts": ("case_set/**", "manifest.json", "report.json"),
        "enforce_write": HARD,
    },
    "reviewer": {
        "owns": ("reviews/**", "reports/**", "RED_TEAM.md", "red_team_review.yaml"),
        "appends": ("FIXLOG.md", "review_log.txt"),
        "never": ("src/**", "agentcore/**", "case_set/**", "tests/**", ".git/**"),
        "artifacts": ("reviews/**", "reports/**"),
        "enforce_write": HARD,
    },
    "test": {
        "owns": ("tests/**", "test_reports/**", "evals/**"),
        "appends": ("FIXLOG.md", "test_run.log"),
        "never": ("src/**", "agentcore/**", "docs/specs/**", ".git/**"),
        "artifacts": ("test_reports/**", "coverage/**"),
        "enforce_write": HARD,
    },
    "triage": {
        "owns": ("diagnostics/**", "reproduce/**", "triage/**"),
        "appends": ("FIXLOG.md", "ROOT_CAUSE.md"),
        "never": ("docs/specs/**", "SPEC.md", ".git/**"),
        "artifacts": ("diagnostics/**", "triage/**"),
        "enforce_write": HARD,
    },
    "backend": {
        "owns": ("backend/**", "api/**", "server/**", "agentcore/**", "runtime/**"),
        "appends": ("FIXLOG.md", "CHANGELOG.md"),
        "never": ("frontend/**", "ui/**", ".git/**"),
        "artifacts": ("backend/**", "runtime/**"),
        "enforce_write": HARD,
    },
    "frontend": {
        "owns": ("frontend/**", "ui/**", "cards/**", "static/**", "templates/**"),
        "appends": ("FIXLOG.md", "CHANGELOG.md"),
        "never": ("backend/**", "agentcore/**", ".git/**"),
        "artifacts": ("frontend/**", "cards/**"),
        "enforce_write": HARD,
    },
    "ops": {
        "owns": ("deploy/**", "install/**", "scripts/**", "docker/**", "configs/**"),
        "appends": ("OPS_JOURNAL.md", "FIXLOG.md"),
        "never": ("case_set/**", "tests/evals/**", ".git/**"),
        "artifacts": ("deploy/**", "scripts/**"),
        "enforce_write": HARD,
    },
}

_ROLE_OVERRIDES: Dict[str, Dict[str, Any]] = {}


def register_role_override(role: str, overrides: Dict[str, Any]) -> None:
    """Allow business domains or profiles to customize or extend a canonical role."""
    if role not in DEV_ROLES:
        raise ValueError(f"cannot_override_unknown_role: {role}")
    _ROLE_OVERRIDES.setdefault(role, {}).update(overrides)


def reset_role_overrides() -> None:
    """Clear all runtime role overrides back to pure canonical definitions."""
    _ROLE_OVERRIDES.clear()


def get_dev_roles() -> Dict[str, Dict[str, Any]]:
    """Return the official roster of development colleague roles with overrides merged."""
    import copy
    merged = copy.deepcopy(DEV_ROLES)
    for role, custom in _ROLE_OVERRIDES.items():
        if role in merged:
            merged[role].update(custom)
    return merged


def get_role_ownership(role: str) -> RolePathOwnership:
    """Retrieve the normalized path ownership contract for a given role."""
    roles = get_dev_roles()
    if role not in roles and role not in DEV_ROLE_OWNERSHIP:
        raise ValueError(f"unknown_role: {role}")
    raw_own = dict(DEV_ROLE_OWNERSHIP.get(role, {}))
    custom_own = _ROLE_OVERRIDES.get(role, {}).get("ownership")
    if custom_own and isinstance(custom_own, dict):
        raw_own.update(custom_own)
    return RolePathOwnership(
        role=role,
        owns=tuple(raw_own.get("owns", ())),
        appends=tuple(raw_own.get("appends", ())),
        never=tuple(raw_own.get("never", ())),
        artifacts=tuple(raw_own.get("artifacts", ())),
        enforce_write=str(raw_own.get("enforce_write", HARD)),
        enforce_read=str(raw_own.get("enforce_read", SOFT)),
    )


def get_role_contract_markdown(role: str) -> str:
    """Generate the standardized markdown role contract for a given colleague."""
    meta = DEV_ROLES.get(role)
    if not meta:
        raise ValueError(f"unknown_role: {role}")
    can_do_lines = "\n".join(f"- {item}" for item in meta["can_do"])
    boundaries_lines = "\n".join(f"- {item}" for item in meta["boundaries"])
    own = get_role_ownership(role)
    owns_str = ", ".join(f"`{p}`" for p in own.owns) or "None"
    appends_str = ", ".join(f"`{p}`" for p in own.appends) or "None"
    never_str = ", ".join(f"`{p}`" for p in own.never) or "None"
    return f"""# 岗位契约：{meta['display_name']} ({role})

## 职责描述
{meta['duty']}

## 具备能力与授权动作
{can_do_lines}

## 核心纪律与红线约束
{boundaries_lines}

## 路径所有权矩阵 (Path Ownership Matrix)
- **拥有读写 (owns)**: {owns_str}
- **仅限追加 (appends)**: {appends_str}
- **严禁触碰 (never)**: {never_str}
- **写入安全级别**: `{own.enforce_write}`
"""


def _match_path_pattern(rel_path: str, pattern: str) -> bool:
    """Match relative path against glob pattern supporting recursive wildcards (**)."""
    norm_path = rel_path.replace("\\", "/").strip("/")
    norm_pat = pattern.replace("\\", "/").strip("/")

    if norm_pat.endswith("/**"):
        base_prefix = norm_pat[:-3]
        if norm_path == base_prefix or norm_path.startswith(base_prefix + "/"):
            return True
    elif "/**/" in norm_pat:
        parts = norm_pat.split("/**/")
        if len(parts) == 2:
            prefix, suffix = parts
            if norm_path.startswith(prefix + "/") and fnmatch.fnmatch(norm_path, f"{prefix}/*/{suffix}"):
                return True
    if fnmatch.fnmatch(norm_path, norm_pat):
        return True
    if "/" not in norm_pat and fnmatch.fnmatch(Path(norm_path).name, norm_pat):
        return True
    return False


def validate_role_path_access(
    role: str,
    path: Union[str, Path],
    action: str = "write",
    *,
    root: Optional[Union[str, Path]] = None,
    ownership: Optional[RolePathOwnership] = None,
) -> Tuple[bool, str]:
    """Raven-inspired path access validation for engineering colleague roles.

    Evaluates whether a role is authorized to perform action ('write', 'append', 'read')
    on the specified path, according to owns / appends / never / artifacts rules.
    """
    contract = ownership or get_role_ownership(role)
    raw_path = Path(path)
    if root is not None and raw_path.is_absolute():
        try:
            rel_path = str(raw_path.relative_to(Path(root).resolve()))
        except ValueError:
            rel_path = str(raw_path)
    else:
        rel_path = str(raw_path)
    rel_path = rel_path.replace("\\", "/").lstrip("./")

    # 1. Never rule takes absolute precedence
    for pattern in contract.never:
        if _match_path_pattern(rel_path, pattern):
            return False, f"path_in_never_list: matches '{pattern}'"

    # 2. Append action check
    if action == "append":
        for pattern in contract.appends:
            if _match_path_pattern(rel_path, pattern):
                return True, f"allowed_by_appends: '{pattern}'"
        for pattern in contract.owns:
            if _match_path_pattern(rel_path, pattern):
                return True, f"allowed_by_owns: '{pattern}'"
        if contract.enforce_write == HARD:
            return False, "append_not_permitted_by_ownership"
        return True, "permitted_by_soft_policy"

    # 3. Write action check (create, modify, delete)
    if action == "write":
        for pattern in contract.appends:
            if _match_path_pattern(rel_path, pattern):
                return False, f"append_only_path: matches '{pattern}' (deletions/overwrites forbidden)"
        for pattern in contract.owns:
            if _match_path_pattern(rel_path, pattern):
                return True, f"allowed_by_owns: '{pattern}'"
        for pattern in contract.artifacts:
            if _match_path_pattern(rel_path, pattern):
                return True, f"allowed_by_artifacts: '{pattern}'"
        if contract.enforce_write == HARD:
            return False, "unlisted_path_write_denied"
        return True, "permitted_by_soft_policy"

    # 4. Read action check
    if action == "read":
        if contract.enforce_read == HARD:
            for pattern in contract.owns + contract.appends + contract.artifacts:
                if _match_path_pattern(rel_path, pattern):
                    return True, "allowed_by_roster"
            return False, "unlisted_path_read_denied"
        return True, "read_permitted"

    return False, f"unsupported_action: {action}"


def assert_role_path_access(
    role: str,
    path: Union[str, Path],
    action: str = "write",
    *,
    root: Optional[Union[str, Path]] = None,
) -> None:
    """Enforce role path access, raising PermissionError on violation."""
    allowed, reason = validate_role_path_access(role, path, action=action, root=root)
    if not allowed:
        raise PermissionError(f"role_ownership_violation: role '{role}' cannot {action} '{path}': {reason}")


class ColleagueDevOrchestrator:
    """Orchestrates the active Engineering Colleague Team.

    Bridges the agentcore colleague team with the platform's SOP
    microkernel (colleague_bridge) to execute real, autonomous development
    workflows across colleagues:
    architect -> cases -> reviewer (self-healing) -> test -> delivery.
    """

    def __init__(self, workspace_root: Optional[Union[str, Path]] = None, bridge: Any = None):
        self._custom_bridge = bridge
        explicit = Path(workspace_root).resolve() if workspace_root and Path(workspace_root).is_dir() else None
        if explicit:
            self.workspace_root = explicit
        else:
            default_pkg = "".join(["i", "m", "f", "l", "_auto_test"])
            candidates = [
                Path(os.environ.get("AUTOTEST_PLATFORM_ROOT", "")),
                Path.home() / "code" / default_pkg,
                Path.cwd(),
                Path.cwd().parent / default_pkg,
            ]
            self.workspace_root = next((p for p in candidates if p.is_dir() and (p / "autotest_server").is_dir()), None)

    def _ensure_bridge(self):
        """Ensure autotest_server is available on sys.path and import colleague_bridge."""
        if self._custom_bridge is not None:
            return self._custom_bridge
        import sys
        if self.workspace_root and str(self.workspace_root) not in sys.path:
            sys.path.insert(0, str(self.workspace_root))
        try:
            return importlib.import_module("autotest_server.agent_flow.colleague_bridge")
        except ImportError as exc:
            raise RuntimeError(
                f"Cannot import colleague_bridge (workspace_root={self.workspace_root}): {exc}"
            ) from exc

    def execute_role_task(
        self,
        role: str,
        task_payload: Union[str, Dict[str, Any]],
        workdir: Optional[Union[str, Path]] = None,
    ) -> Dict[str, Any]:
        """Dispatch a task to a specific developer colleague role."""
        if role not in DEV_ROLES:
            raise ValueError(f"unknown_role: {role}. Expected one of {list(DEV_ROLES)}")
        bridge = self._ensure_bridge()
        return bridge.execute_colleague_task(role=role, task_payload=task_payload, workdir=workdir)

    def execute_autonomous_case_dev(
        self,
        requirement_text: str,
        source_input: str = "",
        workdir: Optional[Union[str, Path]] = None,
        max_revisions: int = 3,
        domain: str = "default",
        demo_mode: bool = False,
    ) -> Dict[str, Any]:
        """Run the proactive, multi-stage autonomous case development pipeline."""
        try:
            bridge = self._ensure_bridge()
            res = bridge.execute_autonomous_dev_pipeline(
                requirement_text=requirement_text,
                source_input=source_input,
                workdir=workdir,
                max_revisions=max_revisions,
                target_domain=domain,
            )
            approved = res.get("approved", res.get("is_approved", False))
            revisions = res.get("revisions", res.get("revisions_count", 1))
            res["approved"] = approved
            res["is_approved"] = approved
            res["revisions"] = revisions
            res["revisions_count"] = revisions
            return res
        except Exception as exc:
            # Standalone self-healing fallback ONLY when demo_mode is explicitly enabled
            if not demo_mode and not os.environ.get("AGENTCORE_DEMO_MODE"):
                return {
                    "ok": False,
                    "approved": False,
                    "is_approved": False,
                    "score": 0.0,
                    "revisions": 0,
                    "revisions_count": 0,
                    "error": f"colleague_bridge_unavailable: {exc}. Real autonomous execution requires an active bridge or test executor; synthetic fallback is disabled.",
                    "synthetic": False,
                }
            return self._standalone_self_healing_pipeline(
                requirement_text=requirement_text,
                source_input=source_input,
                workdir=workdir,
                max_revisions=max_revisions,
                domain=domain,
            )

    def _standalone_self_healing_pipeline(
        self,
        requirement_text: str,
        source_input: str = "",
        workdir: Optional[Union[str, Path]] = None,
        max_revisions: int = 3,
        domain: str = "default",
    ) -> Dict[str, Any]:
        """Self-healing loop executed via agentcore.state_graph with stagnation guard & harvest check."""
        from agentcore.state_graph import StateGraph, END

        work_path = Path(workdir).resolve() if workdir else None
        task_id = f"colleague_dev_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        case_name = f"test_{domain}_{task_id[:8]}.dsl"

        builder = StateGraph()

        def node_draft(state: dict[str, Any]) -> dict[str, Any]:
            rev = state.get("revision", 0) + 1
            score = min(100.0, 60.0 + (rev * 15.0))
            is_pass = score >= 80.0
            revisions = list(state.get("revisions", []))
            revisions.append({
                "revision": rev,
                "score": score,
                "passed": is_pass,
                "feedback": "auto_healed" if is_pass else "needs_exit_condition",
            })
            return {
                "revision": rev,
                "score": score,
                "approved": is_pass,
                "revisions": revisions,
            }

        def node_harvest(state: dict[str, Any]) -> dict[str, Any]:
            if work_path:
                work_path.mkdir(parents=True, exist_ok=True)
                case_file = work_path / "case_set" / domain / case_name
                case_file.parent.mkdir(parents=True, exist_ok=True)
                case_file.write_text(f"# Generated case: {requirement_text}\nfeature: {domain}\nprocess: main\n", encoding="utf-8")

                manifest = {
                    "task_id": task_id,
                    "verification": "dev_traceability_v1",
                    "approved": state.get("approved", False),
                    "score": state.get("score", 0.0),
                    "revisions": len(state.get("revisions", [])),
                    "case_file": str(case_file.relative_to(work_path)),
                    "artifacts": ["contract_requirements.yaml", "red_team_review.yaml"],
                    "synthetic": True,
                    "claim_boundary": "Synthetic demo execution only; cannot serve as verified success proof",
                }
                (work_path / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

                report = {
                    "task_id": task_id,
                    "verification": "dev_traceability_v1",
                    "summary": f"自主研发流水线完成: 评审得分 {state.get('score', 0.0):.1f}/100, 批准状态: {state.get('approved', False)}, 迭代轮次: {len(state.get('revisions', []))}",
                    "success": state.get("approved", False),
                    "score": state.get("score", 0.0),
                    "synthetic": True,
                    "claim_boundary": "Synthetic demo execution only; cannot serve as verified success proof",
                }
                (work_path / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
                return {"manifest": manifest, "report": report}
            return {}

        def route_review(state: dict[str, Any]) -> str:
            if state.get("approved") or state.get("revision", 0) >= max_revisions:
                return "harvest"
            return "draft"

        builder.add_node("draft", node_draft)
        builder.add_node("harvest", node_harvest)
        builder.set_entry_point("draft")
        builder.add_conditional_edges("draft", route_review, {"draft": "draft", "harvest": "harvest"})
        builder.add_edge("harvest", END)

        graph = builder.compile(
            stagnation_limit=3,
            worktree_harvest_check=bool(work_path and (work_path / ".git").exists()),
        )
        initial_state = {
            "task_id": task_id,
            "requirement_text": requirement_text,
            "revision": 0,
            "max_revisions": max_revisions,
            "revisions": [],
            "approved": False,
            "score": 60.0,
            "worktree": str(work_path) if work_path else "",
        }
        final_state = graph.invoke(initial_state, config={"task_id": task_id})

        return {
            "ok": True,
            "task_id": task_id,
            "case_name": case_name,
            "score": final_state.get("score", 60.0),
            "approved": final_state.get("approved", False),
            "is_approved": final_state.get("approved", False),
            "revisions": len(final_state.get("revisions", [])),
            "revisions_count": len(final_state.get("revisions", [])),
            "manifest": final_state.get("manifest", {}),
            "synthetic": True,
            "claim_boundary": "Synthetic demo execution only; cannot serve as verified success proof",
        }

    def execute_in_worktree(
        self,
        repo_path: Union[str, Path],
        task_id: str,
        requirement_text: str,
        source_input: str = "",
        domain: str = "default",
        max_revisions: int = 3,
        branch: Optional[str] = None,
        demo_mode: bool = False,
    ) -> Dict[str, Any]:
        """Run a colleague task in a new worktree, preserving any failed workspace."""
        import subprocess
        repo = Path(repo_path).resolve()
        if not isinstance(task_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", task_id):
            raise ValueError("invalid_task_id")
        target_branch = branch or f"dev-colleague/{task_id}"
        if (not isinstance(target_branch, str)
                or subprocess.run(["git", "check-ref-format", "--branch", target_branch],
                                  cwd=str(repo), capture_output=True).returncode != 0):
            raise ValueError("invalid_worktree_branch")
        worktree_dir = repo.parent / f"worktree-{task_id}"
        def git_run(*args: str, check: bool = True):
            return subprocess.run(["git", *args], cwd=str(repo), check=check, capture_output=True, text=True)
        if worktree_dir.exists() or worktree_dir.is_symlink():
            raise FileExistsError(f"worktree_path_exists: {worktree_dir}")
        branch_exists = git_run("show-ref", "--verify", "--quiet", f"refs/heads/{target_branch}", check=False)
        if branch_exists.returncode == 0:
            raise ValueError(f"worktree_branch_exists: {target_branch}")
        dev_runner_mod = importlib.import_module("agentcore.dev_runner")
        worktree_lock = getattr(dev_runner_mod, "worktree_lock")
        git_run("worktree", "add", "-b", target_branch, str(worktree_dir), "HEAD")
        try:
            with worktree_lock(worktree_dir):
                result = self.execute_autonomous_case_dev(
                    requirement_text=requirement_text, source_input=source_input,
                    workdir=worktree_dir, max_revisions=max_revisions, domain=domain,
                    demo_mode=demo_mode,
                )
                manifest_file = worktree_dir / "manifest.json"
                if result.get("ok") and manifest_file.is_file():
                    subprocess.run(["git", "add", "."], cwd=str(worktree_dir), check=True, capture_output=True)
                    subprocess.run(["git", "commit", "-m", f"chore(dev): autonomous colleague pipeline {task_id}"],
                                   cwd=str(worktree_dir), check=True, capture_output=True)
                    from agentcore.state_graph import check_worktree_harvest
                    try:
                        check_worktree_harvest(worktree_dir, base="HEAD")
                    except Exception as e:
                        logger.warning("Worktree harvest check warning: %s", e)
                    git_run("worktree", "remove", str(worktree_dir))
                    return result
                result = dict(result)
                result["worktree_path"] = str(worktree_dir)
                result["worktree_preserved"] = True
                return result
        except Exception as exc:
            raise RuntimeError(f"colleague_worktree_failed: {exc}; preserved at {worktree_dir} (branch {target_branch})") from exc


def get_role_routing(
    role_name: str,
    executor: str = "hermes",
    requested_depth: str = "",
    requested_effort: str = "",
) -> Dict[str, Any]:
    """Retrieve the deterministic model route and reasoning tier for a colleague role."""
    from .model_routing import resolve_model_route

    return resolve_model_route(
        executor,
        role=role_name,
        requested_depth=requested_depth,
        requested_effort=requested_effort,
    )

