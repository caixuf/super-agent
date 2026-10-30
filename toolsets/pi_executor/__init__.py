"""Pi 执行器 —— 把外部 coding-agent CLI 作为**可调用的子进程适配器**接进宿主智能体。

这是适配器，不是调度器：它不取代 `delegate_task`，也不接管任何宿主的调度权威。
调用方（父代理）负责目标与验收，本模块只负责：

1. 在**隔离的** agent dir / 工作目录里，以参数数组（不 shell 拼接）拉起 Pi；
2. 以 JSON 事件流为准判定「这次调用到底成没成」（exit 0 ≠ 成功）；
3. 超时 / 取消时**清理整棵进程**，不留孤儿 node；
4. 产出**结构化回执**：实际模型、退出码、完成状态、最终输出、工具活动。
   —— 回执里**不含** prompt 与工具参数，避免把会话内容泄进普通日志。

边界（红线）：
    * 绝不写用户全局 ``~/.pi`` —— 一切走独立的 ``PI_CODING_AGENT_DIR``。
    * 绝不 log / print 凭据；凭据只在进程内存在，经环境变量传给子进程。
    * 绝不把 Pi 变成第二个调度器 —— 它只在调用方给定的临时目录里读写产物。
"""

from __future__ import annotations

from .driver import (
    PiResult,
    PiRunError,
    PiTimeoutError,
    build_argv,
    parse_jsonl,
    resolve_cli_path,
    resolve_node,
    run_pi,
    summarize_events,
)
from .ccp_config import (
    CcpCredentials,
    resolve_ccp_credentials,
    write_models_json,
)

__all__ = [
    "PiResult",
    "PiRunError",
    "PiTimeoutError",
    "build_argv",
    "parse_jsonl",
    "resolve_cli_path",
    "resolve_node",
    "run_pi",
    "summarize_events",
    "CcpCredentials",
    "resolve_ccp_credentials",
    "write_models_json",
]