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
import sys


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
            result = agent.run_conversation(prompt)
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

