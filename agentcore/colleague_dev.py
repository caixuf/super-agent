"""agentcore.colleague_dev — Official definitions and contracts for the IMFL Development Team.

Replaces the legacy 'read-only' constraint with full-fledged engineering colleague roles
equipped with coding tools (read, write, edit, bash) operating inside isolated worktrees.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

# Standard roster of the IMFL Engineering Colleague Team
DEV_ROLES: Dict[str, Dict[str, Any]] = {
    "architect": {
        "display_name": "需求边界与架构师",
        "duty": "纯黑盒拆解自然语言需求，提取因果关系与测试蓝图，严格防止实现代码污染黑盒边界",
        "can_do": [
            "分析需求原文因果链",
            "提取断言期望与前置条件",
            "产出 ContractRequirementsArtifact",
            "裁决需求与现有代码冲突",
        ],
        "boundaries": [
            "黑盒原则：不得首先阅读业务实现源码",
            "不得自行妥协产品需求规范",
        ],
    },
    "cases": {
        "display_name": "IMFL用例开发大师",
        "duty": "逆向 C++ CM 宏、状态机跳变与脉冲时序，编写合法扁平流式 .imfl 用例与 YAML 入口",
        "can_do": [
            "逆向状态机条件宏与脉冲保持时长",
            "编写合法 6 大原语 .imfl 用例代码",
            "配置 YAML 入口与 topic 声明",
            "根据红队反馈自动修编 DSL",
        ],
        "boundaries": [
            "严禁使用大括号、宏定义、函数嵌套等反模式语法",
            "300ms 以内短脉冲前禁止插入 sleep",
            "必须包含退出条件与反向抑制断言",
        ],
    },
    "reviewer": {
        "display_name": "红队挑刺评审员",
        "duty": "严把代码与用例质量关，执行语法守卫、脱窗时序防死锁检查与正反例覆盖核验",
        "can_do": [
            "审查 5 大禁止反模式语法",
            "校验短脉冲时序与 sleep 脱窗缺陷",
            "核验正反例与退出机制覆盖率",
            "给出结构化扣分项与定向修改指导",
        ],
        "boundaries": [
            "宁严勿宽，任何违背 DSL 规范项一律驳回",
            "不得在证据不全时默认 approve",
        ],
    },
    "test": {
        "display_name": "SIL仿真测试执行专家",
        "duty": "在 Docker 仿真环境中编译、驱动回灌用例，执行 GATE1-GATE5 门禁与真实报告归档",
        "can_do": [
            "拉起 Docker 回灌与 MFF 节点",
            "执行 pytest 与 pipeline_check 门禁",
            "采集流式日志与状态机跳变轨迹",
            "生成不可变 HTML/JSON 测试报告",
        ],
        "boundaries": [
            "制品 SHA256 校验不通过时必须抛错终止，严禁回退旧版本",
            "测试结果以真实容器退出码为准，严禁伪造测试结果",
        ],
    },
    "triage": {
        "display_name": "失败定性与归因专家",
        "duty": "对仿真与回灌失败进行机制级诊断，确定性归因为软件 Bug、用例写错或语义变更",
        "can_do": [
            "比对 Cur_state 与 CM 翻转时序",
            "定位 DDS 通信延迟与 CPU 绑核竞争",
            "查验历史先例与决策锚点",
            "产出确定性失败归因结论",
        ],
        "boundaries": [
            "严禁把未复核的推测包装成结论",
            "证据不足时必须如实报告无法确诊",
        ],
    },
    "backend": {
        "display_name": "平台后端开发工程师",
        "duty": "开发与维护 autotest_server 调度框架、REST/SSE API、任务持久化与飞书协同闭环",
        "can_do": [
            "编写 FastAPI / aiohttp 路由与数据模型",
            "加固 task_store SQLite 原子事务",
            "优化 local_executor 严格串行 FIFO 队列",
            "维护飞书多维表格双向同步",
        ],
        "boundaries": [
            "严禁绕开锁拉起不受控子进程",
            "严禁绕过连接池直接修改任务数据库",
        ],
    },
    "frontend": {
        "display_name": "表现层与卡片交互师",
        "duty": "负责飞书 Card 2.4 原生表格排版、图文渲染优化与流式交互体验",
        "can_do": [
            "构造合法的飞书卡片 2.4 schema",
            "优化 Markdown 至原生 table/div 降级渲染",
            "调优 Playwright 图文导出排版与分页",
        ],
        "boundaries": [
            "严格遵守 Card 2.4 规范，禁发 1.0 note 等弃用标签",
            "保证单卡生命周期唯一性，禁止二次触发退役卡",
        ],
    },
    "ops": {
        "display_name": "平台运维与巡检专家",
        "duty": "监控系统服务存活、工作区清理、部署同步与 UUID 幂等飞书告警",
        "can_do": [
            "巡检 systemd 用户服务健康度",
            "监控任务租约与看门狗心跳",
            "执行两机代码同步与工作树清理",
            "向唯一运维群投递关键漂移告警",
        ],
        "boundaries": [
            "告警必须带 SHA256 UUID 幂等键，严禁重复骚扰群聊",
            "严禁泄露密钥、Token 与飞书 Secret",
        ],
    },
}


def get_dev_roles() -> Dict[str, Dict[str, Any]]:
    """Return the official roster of development colleague roles."""
    return DEV_ROLES


def get_role_contract_markdown(role: str) -> str:
    """Generate the standardized markdown role contract for a given colleague."""
    meta = DEV_ROLES.get(role)
    if not meta:
        raise ValueError(f"unknown_role: {role}")
    can_do_lines = "\n".join(f"- {item}" for item in meta["can_do"])
    boundaries_lines = "\n".join(f"- {item}" for item in meta["boundaries"])
    return f"""# 岗位契约：{meta['display_name']} ({role})

## 职责描述
{meta['duty']}

## 具备能力与授权动作
{can_do_lines}

## 核心纪律与红线约束
{boundaries_lines}
"""


class ColleagueDevOrchestrator:
    """Orchestrates the active IMFL Engineering Colleague Team.

    Bridges the agentcore colleague team with the platform's Meta-Lang SOP
    microkernel (autotest_server.agent_flow.colleague_bridge) to execute real,
    autonomous development workflows across colleagues:
    architect -> cases -> reviewer (self-healing) -> test -> delivery.
    """

    def __init__(self, workspace_root: Optional[Union[str, Path]] = None):
        candidates = [
            Path("/home/caixuf/code/imfl_auto_test"),
            Path("/home/emily/imfl_auto_test"),
            Path.cwd(),
            Path.cwd().parent / "imfl_auto_test",
        ]
        if workspace_root and Path(workspace_root).is_dir():
            self.workspace_root = Path(workspace_root).resolve()
        else:
            self.workspace_root = next((p for p in candidates if (p / "autotest_server").is_dir()), None)

    def _ensure_bridge(self):
        """Ensure autotest_server is available on sys.path and import colleague_bridge."""
        import sys
        if self.workspace_root and str(self.workspace_root) not in sys.path:
            sys.path.insert(0, str(self.workspace_root))
        try:
            from autotest_server.agent_flow import colleague_bridge
            return colleague_bridge
        except ImportError as exc:
            raise RuntimeError(
                f"Cannot import autotest_server.agent_flow.colleague_bridge (workspace_root={self.workspace_root}): {exc}"
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
        domain: str = "tmas",
    ) -> Dict[str, Any]:
        """Run the proactive, multi-stage autonomous case development pipeline."""
        bridge = self._ensure_bridge()
        return bridge.execute_autonomous_dev_pipeline(
            requirement_text=requirement_text,
            source_input=source_input,
            workdir=workdir,
            max_revisions=max_revisions,
            target_domain=domain,
        )

