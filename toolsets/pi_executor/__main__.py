"""手动跑一次的 CLI：``python3 -m <your_package>.pi_executor ...``。

只为「真实跑通 / 现场排障」用；不参与任何宿主调度。
回执打印的是结构化字段，**不含** prompt 与工具参数。

示例（隔离临时目录，读 fixture、写产物）::

    SC="${HERMES_HOME:-$HOME/.hermes}/cache/scratch"
    python3 -m <your_package>.pi_executor \\
        --workdir "$SC/pi_work" --agent-dir "$SC/pi_home" \\
        --prompt "Read fixture.txt then create out.txt with exactly its content." \\
        --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .ccp_config import (
    CCP_KEY_ENV_VAR,
    DEFAULT_CCP_ALIAS,
    resolve_ccp_credentials,
    write_models_json,
)
from .driver import PiRunError, run_pi


def _default_scratch() -> Path:
    """默认隔离根目录：优先 $HERMES_HOME，其次按画像名拼（画像名不硬编码）。"""
    home = os.environ.get("HERMES_HOME")
    if home:
        return Path(home).expanduser() / "cache" / "scratch" / "pi_executor"
    profile = os.environ.get("HERMES_PROFILE", "default")
    return Path.home() / ".hermes" / "profiles" / profile / "cache" / "scratch" / "pi_executor"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pi_executor", description=__doc__)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--workdir", default=None, help="Pi 的工作目录（读写产物的地方）")
    parser.add_argument("--agent-dir", default=None, help="隔离的 PI_CODING_AGENT_DIR")
    parser.add_argument("--model", default=DEFAULT_CCP_ALIAS)
    parser.add_argument("--provider", default="ccp")
    parser.add_argument("--tools", default="read,write")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--json", action="store_true", help="打印完整回执 JSON")
    args = parser.parse_args(argv)

    scratch = _default_scratch()
    workdir = Path(args.workdir).expanduser() if args.workdir else scratch / "work"
    agent_dir = Path(args.agent_dir).expanduser() if args.agent_dir else scratch / "home"
    workdir.mkdir(parents=True, exist_ok=True)

    creds = resolve_ccp_credentials(alias=args.model)
    write_models_json(agent_dir, base_url=creds.base_url, alias=creds.alias)

    try:
        result = run_pi(
            args.prompt,
            workdir=workdir,
            agent_dir=agent_dir,
            model=args.model,
            provider=args.provider,
            tools=args.tools,
            timeout_s=args.timeout,
            env_extra={CCP_KEY_ENV_VAR: creds.api_key},
        )
    except PiRunError as exc:
        print(f"[pi_executor] 前置条件失败: {exc}", file=sys.stderr)
        return 2

    payload = result.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"ok={payload['ok']} exit={payload['exit_code']} settled={payload['settled']} "
              f"stop={payload['stop_reason']} model={payload['model']} tools={payload['tool_calls']}")
        if payload["error_message"]:
            print(f"error: {payload['error_message']}")
        if payload["final_text"]:
            print(f"final_text: {payload['final_text'][:500]}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())