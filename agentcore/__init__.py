"""agentcore —— 通用多智能体与记忆引擎。

定位：一个**与业务无关**的通用多智能体与长期记忆核心。存储、召回、分桶、
冲突判定、租约恢复、执行门禁、失败分类治理与子智能体协同协议在此实现；
具体领域词、专有代际与宿主环境均由外部注入。

本包不出现任何具体企业的领域名、代际名或专有名词，这条约束由测试
``tests/test_business_neutrality.py`` 强制执行。
"""

from __future__ import annotations

from .failure_mender import (
    build_escalation_request,
    classify_failure,
    decide_failure,
    escalate_timeout,
    validate_failure_decision,
)
from .memory import (
    MemoryConfig,
    configure,
    flush_background,
    get_config,
    recall_facts,
    reset_config,
    write_fact,
)
from .subagent_protocol import (
    GOAL_ECHO_PROTOCOL,
    SEARCH_DISCIPLINE_PROTOCOL,
    VERIFICATION_STOP_PROTOCOL,
    SubagentUnit,
    assemble_subagent_prompt,
    contain_cascading_failure,
    detect_boundary_conflicts,
    validate_subagent_result,
)
from .verification_gate import (
    VerificationCheck,
    evaluate_verification_checks,
    normalize_verification_check,
    validate_verification_receipt,
)

__version__ = "0.2.0"

__all__ = [
    "MemoryConfig",
    "configure",
    "get_config",
    "reset_config",
    "write_fact",
    "recall_facts",
    "flush_background",
    "classify_failure",
    "decide_failure",
    "validate_failure_decision",
    "build_escalation_request",
    "escalate_timeout",
    "VerificationCheck",
    "normalize_verification_check",
    "evaluate_verification_checks",
    "validate_verification_receipt",
    "SubagentUnit",
    "detect_boundary_conflicts",
    "assemble_subagent_prompt",
    "validate_subagent_result",
    "contain_cascading_failure",
    "GOAL_ECHO_PROTOCOL",
    "VERIFICATION_STOP_PROTOCOL",
    "SEARCH_DISCIPLINE_PROTOCOL",
    "__version__",
]