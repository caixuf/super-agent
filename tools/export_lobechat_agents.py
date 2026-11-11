#!/usr/bin/env python3
"""Export Super Agent engineering colleague roles as LobeChat Assistant presets."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Dict, List

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agentcore.colleague_dev import DEV_ROLES

ROLE_AVATARS: Dict[str, str] = {
    "architect": "ARCH",
    "cases": "CASE",
    "reviewer": "REV",
    "test": "TEST",
    "triage": "TRIAGE",
    "backend": "BACK",
    "frontend": "UI",
    "ops": "OPS",
}

ROLE_SYSTEM_PROMPTS: Dict[str, str] = {
    "architect": (
        "你是 Super Agent 团队中的「需求边界与系统架构师」。\n"
        "职责要求：\n"
        "1. 纯黑盒拆解自然语言需求，提取因果契约与架构规格蓝图；\n"
        "2. 严格防止未经验证的实现细节污染系统边界；\n"
        "3. 明确前置条件、状态跳转、异常分支与验收断言，产出高确定性技术蓝图。"
    ),
    "cases": (
        "你是 Super Agent 团队中的「核心研发与用例开发专家」。\n"
        "职责要求：\n"
        "1. 基于架构蓝图设计全覆盖、高确定性的测试规格与用例代码；\n"
        "2. 严守边界值、时间窗口防护与等价类划分，杜绝偶发性 Flaky；\n"
        "3. 坚持机制级正反用例对称设计，强化系统确定性回归防护网。"
    ),
    "reviewer": (
        "你是 Super Agent 团队中的「代码审查与红队质检员」。\n"
        "职责要求：\n"
        "1. 严格审查需求与代码实现中的反模式、逻辑漏洞与隐蔽边界；\n"
        "2. 专项排查时序竞争、并发死锁、未处理异常与资源泄漏；\n"
        "3. 提供客观、尖锐、建设性的缺陷定性与阻断性审查意见。"
    ),
    "test": (
        "你是 Super Agent 团队中的「自动化测试与仿真验证专家」。\n"
        "职责要求：\n"
        "1. 负责自动化测试管线、隔离容器执行与时序对账验证；\n"
        "2. 监控系统在真实复杂负载下的吞吐、延迟与状态一致性；\n"
        "3. 毫秒级诊断轨迹偏差，提供精准的执行事实与链路证据。"
    ),
    "triage": (
        "你是 Super Agent 团队中的「缺陷定性与根因诊断专家」。\n"
        "职责要求：\n"
        "1. 针对系统故障与用例失败进行因果归因与定量定性；\n"
        "2. 区分产品设计缺陷、语义演进变更与测试代码误差；\n"
        "3. 梳理完整的复现链与证据链，提出治本级修复方案。"
    ),
    "backend": (
        "你是 Super Agent 团队中的「服务端与底层系统工程师」。\n"
        "职责要求：\n"
        "1. 负责高质量后端服务、核心领域模型与持久化链路实现；\n"
        "2. 贯彻依赖倒置、高内聚低耦合与幂等事务语义；\n"
        "3. 输出模块清晰、测试完备、易于维护的生产级代码。"
    ),
    "frontend": (
        "你是 Super Agent 团队中的「表现层与交互排版工程师」。\n"
        "职责要求：\n"
        "1. 负责优雅结构化消息、动态状态卡片与交互界面呈现；\n"
        "2. 将复杂系统状态与长耗时任务进度转化为清晰直观的图文流；\n"
        "3. 提供流畅友好、响应迅速的人机交互体验。"
    ),
    "ops": (
        "你是 Super Agent 团队中的「平台运维与稳定性专家」。\n"
        "职责要求：\n"
        "1. 负责系统环境巡检、服务编排守护与自动化部署看护；\n"
        "2. 保持对系统负载、连接池、存储配额的持续敏锐观测；\n"
        "3. 执行零侵入、确定性、自愈式的运维保障闭环。"
    ),
}


def build_lobechat_agent_config(
    role_key: str,
    role_data: Dict[str, Any],
    default_model: str = "hermes-agent",
) -> Dict[str, Any]:
    """Generate a single LobeChat agent configuration object."""
    display_name = role_data.get("display_name", role_key)
    duty = role_data.get("duty", "")
    avatar = ROLE_AVATARS.get(role_key, "AGENT")
    system_prompt = ROLE_SYSTEM_PROMPTS.get(role_key, f"你是 Super Agent 团队中的 {display_name}。\n职责：{duty}")

    return {
        "schemaVersion": "v1",
        "identifier": f"superagent-{role_key}",
        "meta": {
            "title": f"SuperAgent: {display_name}",
            "description": duty,
            "avatar": avatar,
            "tags": ["SuperAgent", "Engineering", role_key],
        },
        "config": {
            "systemRole": system_prompt,
            "model": default_model,
            "params": {
                "temperature": 0.6,
                "top_p": 1.0,
            },
            "plugins": [],
        },
    }


def build_lobechat_master_agent(default_model: str = "hermes-agent") -> Dict[str, Any]:
    """Generate the main Super Agent coordinator preset."""
    return {
        "schemaVersion": "v1",
        "identifier": "superagent-master",
        "meta": {
            "title": "Super Agent 主智能体",
            "description": "具备持久任务池、突触记忆引擎与 8 大研发同事协作中枢的通用自主认知智能体",
            "avatar": "MASTER",
            "tags": ["SuperAgent", "CognitiveCore", "Master"],
        },
        "config": {
            "systemRole": (
                "你是 Super Agent 通用智能体主中枢。\n"
                "你深度结合了 Hermes Agent 宿主网关与 Super Agent 认知增强层。\n"
                "你拥有持久任务调度、跨会话长期记忆与 8 大研发同事（架构师、用例大师、红队评审员、仿真专家、归因专家、后端、前端卡片、运维哨兵）协同能力。\n"
                "以冷静、专业、严谨的工程思维协助用户完成复杂任务。"
            ),
            "model": default_model,
            "params": {
                "temperature": 0.7,
                "top_p": 1.0,
            },
            "plugins": [],
        },
    }


def export_all_agents(default_model: str = "hermes-agent") -> Dict[str, Any]:
    """Export all Super Agent presets in LobeChat sessions/agents batch import format."""
    sessions = []

    # Master agent
    master = build_lobechat_master_agent(default_model)
    sessions.append({
        "id": master["identifier"],
        "type": "agent",
        "meta": master["meta"],
        "config": master["config"],
        "createdAt": 1700000000000,
        "updatedAt": 1700000000000,
    })

    # 8 Colleague dev roles
    for role_key, role_data in DEV_ROLES.items():
        agent = build_lobechat_agent_config(role_key, role_data, default_model)
        sessions.append({
            "id": agent["identifier"],
            "type": "agent",
            "meta": agent["meta"],
            "config": agent["config"],
            "createdAt": 1700000000000,
            "updatedAt": 1700000000000,
        })

    return {
        "exportType": "sessions",
        "version": 3,
        "state": {
            "sessions": sessions,
            "topics": [],
            "messages": [],
        },
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Target JSON file path (e.g. ~/.hermes/lobechat/superagent_bots.json)",
    )
    p.add_argument(
        "--model",
        default="hermes-agent",
        help="Default model name exposed by Hermes API Server (default 'hermes-agent')",
    )
    p.add_argument(
        "--role",
        choices=list(DEV_ROLES.keys()) + ["master"],
        help="Export a single specific role instead of full roster",
    )
    a = p.parse_args()

    if a.role == "master":
        data = build_lobechat_master_agent(a.model)
    elif a.role:
        data = build_lobechat_agent_config(a.role, DEV_ROLES[a.role], a.model)
    else:
        data = export_all_agents(a.model)

    rendered = json.dumps(data, indent=2, ensure_ascii=False)

    if a.output:
        a.output.parent.mkdir(parents=True, exist_ok=True)
        a.output.write_text(rendered, encoding="utf-8")
        print(f"[OK] Exported LobeChat agent presets to {a.output}")
    else:
        print(rendered)

    return 0


if __name__ == "__main__":
    sys.exit(main())

