#!/usr/bin/env python3
"""Sandboxed in-process Hermes one-shot executor for durable backend workers.

Upstream Hermes CLI entrypoints (`hermes chat` and `hermes -z`) route through the
profile's shared `tui_gateway` WebSocket server (`connect_gateway()`), which:
  1. Executes turns and tool subprocesses inside the host gateway daemon rather
     than inside the worker's AttemptBoundSandboxAdapter (`bwrap`) + subreaper tree;
  2. Fails with `GatewayUnavailableError` (`rc=1`) whenever the gateway daemon is
     restarting or when a colleague profile has no standalone gateway service.

This entrypoint runs inside the Hermes installation's store Python after
`import hermes_bootstrap` (resolved via `hermes --print-runtime-command`) so the
entire `AIAgent` conversation and every tool subprocess execute strictly in-process
inside `bwrap` under `attempt_supervisor.py`.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import dataclasses
import json
import logging
import os
from pathlib import Path
import re
import sys

_MAX_BACKEND_CONTINUATIONS = 2
_BACKEND_CONTINUATION_NUDGE = (
    "请不要停留在过程播报；请立即执行上述下一步工具操作，"
    "并在完成后直接输出面向用户的完整最终结论与结果报告。"
)
_SUMMED_USAGE_KEYS = (
    "api_calls",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "total_tokens",
)
_BACKEND_NARRATION_ZH_RE = re.compile(
    r"(?:^|[。！？.!?\n]\s*)"
    r"(?P<sent>"
    r"(?:[^。！？.!?\n]*[，；,;]\s*)?"
    r"(?P<tail>(?:我)?(?:正在|现在|接下来|下面|随后|先|接着|马上|最后)(?:我)?(?:再|就|来|去)?"
    r"(?:查清|检查|排查|核对|定位|读取|查看|复现|验证|"
    r"解析|抓取|提取|汇总|整理|分析|统计|对比|筛选|拉取|补验|"
    r"生成|输出|编写|构建|继续|"
    r"做(?:一次|一遍|最后一次)?[^。！？.!?\n]{0,20}(?:验证|检查|核对|测试|排查|对比|分析|汇总)|"
    r"运行|跑|执行)"
    r"[^。！？.!?\n]{0,130})[。.…：:]?\s*)$"
)
_BACKEND_NARRATION_EN_RE = re.compile(
    r"(?:^|[.!?\n]\s*)(?:let me now|i will now|now let me|next i(?:'ll| will))"
    r"[^.!?\n]{0,120}[.:…]?\s*$",
    re.IGNORECASE,
)


def _is_incomplete_backend_narration(text: str | None) -> bool:
    """Return True when a short unattended worker turn stopped on a step announcement."""
    t = (text or "").strip()
    if not t or len(t) > 400 or "```" in t or "~~~" in t:
        return False
    if t.startswith("# ") or "\n#" in t or "\n|" in t:
        return False
    if sum(1 for line in t.splitlines() if re.match(r"^\s*(?:[-*+]|\d+\.)\s+", line)) >= 3:
        return False
    if _BACKEND_NARRATION_EN_RE.search(t):
        return True
    match = _BACKEND_NARRATION_ZH_RE.search(t)
    if not match:
        return False
    sent = match.group("sent") or ""
    tail = match.group("tail") or ""
    if sent.lstrip().startswith(">"):
        return False
    if any(cue in sent for cue in (
        "建议", "你", "您", "等待", "待确认", "确认后", "批准", "授权",
        "如果", "暂不", "无需", "不必", "不要", "可以吗", "吗", "？", "?",
        "“", "”", "‘", "’", '"', "'", "`",
    )):
        return False
    return not any(cue in tail for cue in (
        "完成", "已经", "已", "完毕", "通过了", "无异常", "结果正常", "没有异常",
        "测试通过", "回归通过", "不能", "不用", "不需要", "结果是", "根因是", "原因是",
    ))


def _merge_continuation_result(base: dict, nxt: dict) -> dict:
    """Combine usage counters across in-process continuation turns."""
    merged = dict(base)
    merged.update(nxt)
    for key in _SUMMED_USAGE_KEYS:
        if base.get(key) is not None or nxt.get(key) is not None:
            merged[key] = (base.get(key) or 0) + (nxt.get(key) or 0)
    if base.get("estimated_cost_usd") is not None and nxt.get("estimated_cost_usd") is not None:
        merged["estimated_cost_usd"] = base["estimated_cost_usd"] + nxt["estimated_cost_usd"]
    return merged


def _continue_incomplete_backend_turn(
    agent,
    result: dict,
    *,
    max_continuations: int = _MAX_BACKEND_CONTINUATIONS,
) -> dict:
    """Nudge an unattended worker turn that stopped mid-step; mark partial if still narration."""
    current = dict(result or {})
    attempts = 0
    while (
        attempts < max_continuations
        and current.get("completed") is True
        and not current.get("failed")
        and not current.get("partial")
        and not current.get("interrupted")
        and _is_incomplete_backend_narration(current.get("final_response"))
    ):
        attempts += 1
        nxt = agent.run_conversation(
            _BACKEND_CONTINUATION_NUDGE,
            conversation_history=current.get("messages"),
        )
        if not isinstance(nxt, dict):
            break
        current = _merge_continuation_result(current, nxt)
    if (
        current.get("completed") is True
        and not current.get("failed")
        and not current.get("partial")
        and not current.get("interrupted")
        and _is_incomplete_backend_narration(current.get("final_response"))
    ):
        current["completed"] = False
        current["partial"] = True
        current["turn_exit_reason"] = "incomplete_narration"
    return current


def _configure_worker_logging() -> None:
    """Enable file-backed `agent.log` / `errors.log` while keeping stdout/stderr clean."""
    try:
        import hermes_logging

        hermes_logging.setup_logging()
    except Exception:
        pass
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, logging.StreamHandler) and getattr(
            handler, "stream", None
        ) in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__):
            root.removeHandler(handler)


def _discover_runtime_extensions(logger: logging.Logger) -> None:
    """Load profile plugins and shell hooks in-process before constructing `AIAgent`."""
    try:
        from hermes_cli.plugins import discover_plugins

        discover_plugins()
    except Exception:
        logger.debug("in-process plugin discovery failed", exc_info=True)
    try:
        from hermes_cli.config import load_config
        from agent.shell_hooks import register_from_config

        register_from_config(load_config(), accept_hooks=True, interactive=False)
    except Exception:
        logger.debug("in-process shell hook registration skipped", exc_info=True)


def _run_inproc_turn(
    prompt: str,
    *,
    model: str | None = None,
    provider: str | None = None,
    reasoning: str | None = None,
    max_turns: int = 1000,
    run_budget: float | None = None,
    usage_file: str | None = None,
) -> int:
    from gateway.session_context import declare_stateless_channel
    from hermes_cli.config import get_hermes_home, load_config
    from hermes_cli.env_loader import load_hermes_dotenv
    from hermes_cli.fallback_config import get_fallback_chain
    from hermes_cli.mcp_startup import ensure_mcp_discovery_before_agent_build
    from hermes_cli.oneshot import (
        _attach_auxiliary_usage,
        _auxiliary_usage,
        _close_agent,
        _create_session_db_for_oneshot,
        _oneshot_clarify_callback,
        _oneshot_exit_code,
        _resolve_model_and_provider,
        _write_usage_file,
    )
    from hermes_cli.runtime_provider import resolve_runtime_with_fallback
    from hermes_cli.tools_config import _get_platform_tools
    from hermes_constants import parse_reasoning_effort, resolve_reasoning_config
    from run_agent import AIAgent

    hermes_home = get_hermes_home()
    try:
        load_hermes_dotenv(project_env=hermes_home / ".env")
    except Exception:
        pass

    _configure_worker_logging()
    logger = logging.getLogger("superagent.hermes_inproc_oneshot")
    _discover_runtime_extensions(logger)
    declare_stateless_channel()

    cfg = load_config()
    effective_model = (
        (model or "").strip()
        or os.environ.get("SUPERAGENT_MODEL_TIER", "").strip()
        or None
    )
    effective_reasoning = (
        (reasoning or "").strip()
        or os.environ.get("SUPERAGENT_REASONING_EFFORT", "").strip()
        or None
    )
    choice = _resolve_model_and_provider(cfg, effective_model, provider)
    session_db = _create_session_db_for_oneshot()

    runtime, fallback_entry = resolve_runtime_with_fallback(
        cfg,
        requested=choice.provider,
        target_model=choice.model or None,
        explicit_base_url=choice.base_url,
        explicit_api_key=choice.api_key,
    )
    if fallback_entry is not None:
        choice = dataclasses.replace(
            choice,
            model=fallback_entry["model"],
            provider=runtime.get("provider"),
            api_mode=None,
        )
    if choice.api_mode:
        runtime["api_mode"] = choice.api_mode

    reasoning_config = resolve_reasoning_config(cfg, choice.model)
    if effective_reasoning:
        parsed_reasoning = parse_reasoning_effort(effective_reasoning)
        if parsed_reasoning is not None:
            reasoning_config = parsed_reasoning

    platform_name = os.environ.get("HERMES_SESSION_PLATFORM", "").strip() or "api_server"
    toolsets_raw = _get_platform_tools(cfg, platform_name)
    if not toolsets_raw:
        toolsets_raw = _get_platform_tools(cfg, "cli")
    toolsets_list = sorted(toolsets_raw) if toolsets_raw else None

    ensure_mcp_discovery_before_agent_build(logger=logger, single_query=True)

    real_stdout = sys.stdout
    real_stderr = sys.stderr
    response: str | None = None
    result: dict = {}
    failure: BaseException | None = None
    agent = None
    relay_session_id = None

    with open(os.devnull, "w", encoding="utf-8") as devnull, redirect_stdout(devnull), redirect_stderr(devnull):
        try:
            agent = AIAgent(
                api_key=runtime.get("api_key"),
                base_url=runtime.get("base_url"),
                provider=runtime.get("provider"),
                requested_provider=runtime.get("requested_provider"),
                api_mode=runtime.get("api_mode"),
                model=choice.model,
                max_iterations=max(1, int(max_turns)),
                run_budget_seconds=float(run_budget) if run_budget and run_budget > 0 else None,
                enabled_toolsets=toolsets_list,
                quiet_mode=True,
                platform=platform_name,
                chat_id=os.environ.get("HERMES_SESSION_CHAT_ID") or None,
                session_db=session_db,
                credential_pool=runtime.get("credential_pool"),
                fallback_model=get_fallback_chain(cfg) or None,
                request_overrides=runtime.get("request_overrides"),
                reasoning_config=reasoning_config,
                cwd=os.getcwd(),
                clarify_callback=_oneshot_clarify_callback,
            )
            agent.suppress_status_output = True
            agent.stream_delta_callback = None
            agent.tool_gen_callback = None

            aux_before = _auxiliary_usage(session_db, None) if usage_file else {}
            relay_session_id = getattr(agent, "session_id", None)
            result = _continue_incomplete_backend_turn(
                agent,
                agent.run_conversation(prompt),
            )
            if usage_file:
                _attach_auxiliary_usage(
                    result,
                    session_db,
                    aux_before,
                    fallback_session_id=getattr(agent, "session_id", None),
                )
            response = result.get("final_response") or ""
        except BaseException as exc:
            failure = exc
        finally:
            _close_agent(agent, session_db, relay_session_id)

    if failure is not None:
        if isinstance(failure, (KeyboardInterrupt, SystemExit)):
            _write_usage_file(usage_file, result, failure=repr(failure))
            raise failure
        _write_usage_file(usage_file, result, failure=str(failure))
        real_stderr.write(f"hermes_inproc_oneshot: agent failed: {failure}\n")
        real_stderr.flush()
        return 1

    _write_usage_file(usage_file, result)
    if response:
        try:
            from agent.message_sanitization import _sanitize_surrogates

            response = _sanitize_surrogates(response)
        except Exception:
            pass
        real_stdout.write(response)
        if not response.endswith("\n"):
            real_stdout.write("\n")
        real_stdout.flush()

    exit_code = _oneshot_exit_code(response, result)
    if exit_code != 0:
        real_stderr.write(
            f"hermes_inproc_oneshot: incomplete or empty turn (rc={exit_code}, "
            f"completed={result.get('completed')}, failed={result.get('failed')}, "
            f"partial={result.get('partial')})\n"
        )
        real_stderr.flush()
    return exit_code


def main(argv: list[str] | None = None) -> int:
    # Set unattended execution flags BEFORE any Hermes tool/approval module imports.
    os.environ["HERMES_YOLO_MODE"] = "1"
    os.environ["HERMES_ACCEPT_HOOKS"] = "1"
    os.environ["HERMES_SINGLE_QUERY_SESSION"] = "1"
    os.environ.setdefault("SUPERAGENT_BACKEND_RUNNER", "1")
    os.environ.setdefault("HERMES_SESSION_PLATFORM", "api_server")

    parser = argparse.ArgumentParser(description="In-process sandboxed Hermes one-shot worker")
    parser.add_argument("--query-file", default="-", help="Prompt file path or '-' for stdin")
    parser.add_argument("--model", default=None)
    parser.add_argument("--provider", default=None)
    parser.add_argument("--reasoning", default=None)
    parser.add_argument("--max-turns", type=int, default=1000)
    parser.add_argument("--run-budget", type=float, default=3600.0)
    parser.add_argument("--usage-file", default=None)
    args = parser.parse_args(argv)

    if args.query_file == "-":
        prompt = sys.stdin.read()
    else:
        prompt = Path(args.query_file).read_text(encoding="utf-8")

    if not prompt or not prompt.strip():
        sys.stderr.write("hermes_inproc_oneshot: empty prompt\n")
        return 1

    return _run_inproc_turn(
        prompt,
        model=args.model,
        provider=args.provider,
        reasoning=args.reasoning,
        max_turns=args.max_turns,
        run_budget=args.run_budget,
        usage_file=args.usage_file,
    )


if __name__ == "__main__":
    raise SystemExit(main())

