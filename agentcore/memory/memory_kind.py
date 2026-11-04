#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆类别：区分「技术经验」与「通用事实」，决定各自走哪条门禁。

为什么必须分（2026-10-23 修正）：
    原先的门禁一刀切要求「代际必须明确（G1/G2）」，这在那个业务场景成立，
    但这是个通用 agent 记忆系统 —— 用户的偏好、人物关系、项目背景、
    环境约定，本来就没有代际。一刀切的结果是：凡是与测试代际无关的真实事实
    全部被拒，记忆库只能装测试经验。

分类（按「怎么验证它」分，不按话题分）：
    technical  技术经验：门禁要求代际 + 可执行 + 查重。
    personal   个人/关系事实：谁是谁、习惯偏好、别名。不要求代际，
               但要求可归属（知道说的是谁），避免无名指代入库。
    context    项目/环境背景：仓库约定、环境配置、流程惯例。不要求代际，
               但要求可执行（得是条能照做的信息，不是感想）。
"""

from __future__ import annotations

import re
from typing import Any, Dict

KIND_TECHNICAL = "technical"
KIND_PERSONAL = "personal"
KIND_CONTEXT = "context"

ALL_KINDS = (KIND_TECHNICAL, KIND_PERSONAL, KIND_CONTEXT)

#: 需要代际才能入库的类别（只有技术经验）
REQUIRES_EBM = (KIND_TECHNICAL,)

#: 人物/关系/偏好
PERSONAL_PATTERNS = (
    r"(我是|我叫|我的名字|本人)",
    r"(回复我|回答我|跟我说话|与我沟通)",
    r"(自称|别名|外号|昵称|花名|称呼|代称|又称|俗称|被称为|指的是|指的是谁)",
    r"(喜欢|习惯|偏好|不喜欢|讨厌|反感)",
    r"(用户名|账号|主机名)",
)

#: 项目/环境背景：仓库约定、流程惯例
CONTEXT_PATTERNS = (
    r"(仓库|分支|目录|路径|配置|环境变量|端口)",
    r"(流程|惯例|规范|约定|规矩|SOP)",
)

#: 技术经验：状态机、门限、时序、构建等技术判断
TECHNICAL_PATTERNS = (
    r"\bR[67](\.\d+)?\b",
    r"(门限|脱窗|时序|状态机|跳转|注入|触发条件)",
    r"(编译|构建|镜像|制品|用例|断言|matcher|topic)",
)


def classify_kind(statement: str, subject: str = "", ebm: str = "") -> str:
    """按「怎么验证」判定类别。技术优先：既提 G2 又说习惯的多半是技术经验。"""
    blob = ("%s %s" % (subject or "", statement or "")).strip()
    if str(ebm or "").strip():
        return KIND_TECHNICAL
    if any(re.search(p, blob) for p in TECHNICAL_PATTERNS):
        return KIND_TECHNICAL
    if any(re.search(p, blob) for p in PERSONAL_PATTERNS):
        return KIND_PERSONAL
    if any(re.search(p, blob) for p in CONTEXT_PATTERNS):
        return KIND_CONTEXT
    # 判不出时按技术处理：技术门禁更严，宁可多拒也不放宽标准
    return KIND_TECHNICAL


def requires_ebm(kind: str) -> bool:
    return kind in REQUIRES_EBM


def describe(kind: str) -> Dict[str, Any]:
    return {
        KIND_TECHNICAL: {"ebm_required": True, "hint": "技术经验：需代际 + 可执行 + 查重"},
        KIND_PERSONAL: {"ebm_required": False, "hint": "人物/偏好：不要求代际，要求可归属"},
        KIND_CONTEXT: {"ebm_required": False, "hint": "项目背景：不要求代际，要求可执行"},
    }.get(kind, {"ebm_required": True, "hint": "未知类别按技术处理"}),

# ---------------------------------------------------------------------------
# observation_class（针类）—— 与 kind 正交，禁止把 kind 折叠进 observation_class
# ---------------------------------------------------------------------------
# kind 回答「怎么验证」（technical/personal/context）。
# observation_class 回答「这条观测对人工扫月报有多尖锐」：
#   decision / bugfix / security_alert / sensitive → needle → 月日志一行
# 其它类或不标 → 不写月日志。标签键固定为 observation_class。

OBS_DECISION = "decision"
OBS_BUGFIX = "bugfix"
OBS_SECURITY_ALERT = "security_alert"
OBS_SENSITIVE = "sensitive"

NEEDLE_CLASSES = frozenset({
    OBS_DECISION,
    OBS_BUGFIX,
    OBS_SECURITY_ALERT,
    OBS_SENSITIVE,
})

TAG_OBSERVATION_CLASS = "observation_class"

_DECISION_HINTS = (
    r"(决定|定了|已定|拍板|采用|改用|改成|换成|确定用|已经确定|已确定|结论是)",
    r"(以后|下次|后续)(都|要|不要|必须|记得)",
    r"(不要|禁止|必须|一律|绝不能)",
)
_BUGFIX_HINTS = (
    r"(踩过|踩坑|踩了|教训是|之前遇到|曾经因为|根因是|bug\s*fix|修复了|修好了)",
    r"(回归失败|断言失败|复现|hotfix)",
)
_SECURITY_HINTS = (
    r"(安全|泄露|泄漏|凭证|密钥|token|secret|权限提升|越权|注入攻击|CVE)",
    r"(security[_\s-]?alert|credential|exfiltrat)",
)
_SENSITIVE_HINTS = (
    r"(身份证|护照|银行卡|密码|口令|私钥|隐私|敏感|手机号|住址)",
    r"(sensitive|PII|个人敏感)",
)


def is_needle(observation_class):
    """observation_class 是否属于针类（决定是否写月日志）。"""
    return str(observation_class or "").strip().lower() in NEEDLE_CLASSES


def classify_observation_class(statement, subject="", tags=None):
    """推断 observation_class；已有 tag 优先；判不出返回 None（非针类）。

    不改写 kind。显式 tag 非法值视为未标注（返回 None），避免脏标签进月报。
    """
    raw = ""
    if isinstance(tags, dict):
        raw = str(tags.get(TAG_OBSERVATION_CLASS) or tags.get("observation_class") or "").strip()
    if raw:
        low = raw.lower()
        return low if low in NEEDLE_CLASSES else None

    blob = ("%s %s" % (subject or "", statement or "")).strip()
    if not blob:
        return None
    if any(re.search(p, blob, re.IGNORECASE) for p in _SECURITY_HINTS):
        return OBS_SECURITY_ALERT
    if any(re.search(p, blob, re.IGNORECASE) for p in _SENSITIVE_HINTS):
        return OBS_SENSITIVE
    if any(re.search(p, blob, re.IGNORECASE) for p in _BUGFIX_HINTS):
        return OBS_BUGFIX
    if any(re.search(p, blob, re.IGNORECASE) for p in _DECISION_HINTS):
        return OBS_DECISION
    return None
