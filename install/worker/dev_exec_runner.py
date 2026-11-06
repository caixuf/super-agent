#!/usr/bin/env python3
"""Two-engine durable job runner for the execution role.

Engine 1 (primary): one bounded Hermes turn on the api_server platform, so the
backend keeps its full toolset (the thin-frontdesk restriction only applies to
human messaging surfaces).

Engine 2 (fallback): the packaged Pi executor (toolsets/pi_executor), an
independent coding-agent CLI with read/bash/edit/write. It is used only when the
primary turn fails, times out, or returns no output, so a primary outage never
silently becomes a lost task.

Both engines receive the same goal. The receipt names the engine that answered.
The runner is invoked as python3 dev_exec_runner.py, reads
{"payload": {"goal": "..."}} from stdin, prints exactly one JSON object on
stdout, and never raises past the worker contract.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

MAX_INPUT = 131072
MAX_GOAL = 12000
MAX_OUTPUT = 8000
MAX_STDERR = 1500
_SSH_HOST_RE = re.compile(r"^[A-Za-z0-9._@-]{1,128}\Z")


def _decorate_goal(goal: str) -> str:
    duty = (os.environ.get("DEV_EXPERT_DUTY") or "").strip()
    target_host = (os.environ.get("DEV_TARGET_HOST") or "").strip()
    if not duty and not target_host:
        return goal
    lines = [
        "[Expert Context]",
        "- Mode: Backend executor (execute directly with terminal/file/MCP tools and return concrete evidence; do NOT call durable_submit).",
        "- Colleague Collaboration & Fault Tolerance: You can inspect the team roster (`colleague_list`), assign/consult specialist colleagues (`colleague_assign` / `colleague_task_status` for architect/cases/reviewer/test/triage/ops/backend/frontend), or spawn parallel subagents (`delegate_task`) when cross-role collaboration or failover speeds up or recovers a task.",
    ]
    if duty:
        lines.append(f"- Duty: {duty}")
    if target_host:
        lines.append(f"- Target Host: {target_host} (execute target operations on this host via SSH/tools when applicable)")
    lines.append("")
    lines.append(goal)
    return "\n".join(lines)[:MAX_GOAL]


def _clean_hermes_output(raw: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw or "")
    parts = re.split(r"(?:^|\n)[─╭]*\s*[☤⚕]\s*Hermes[^\n]*\n", text)
    if len(parts) > 1:
        text = parts[-1]
    text = re.split(r"\n[─]{10,}\s*\n+\s*(?:恢复此会话[：:]|Resume this session:)", text)[0]
    text = re.split(r"\n+\s*(?:恢复此会话[：:]|Resume this session:)", text)[0]
    return text.strip()


def _profile_home() -> Path:
    env = os.environ.get("HERMES_HOME") or os.environ.get("DEV_PROFILE_HOME")
    return Path(env).expanduser() if env else Path.home() / ".hermes"


def _hermes_bin() -> str | None:
    explicit = os.environ.get("DEV_HERMES_BIN")
    if explicit and Path(explicit).is_file():
        return explicit

    # 1. Search PATH
    on_path = shutil.which("hermes")
    if on_path and Path(on_path).is_file():
        return on_path

    # 2. Search user local bin
    local_bin = Path.home() / ".local" / "bin" / "hermes"
    if local_bin.is_file():
        return str(local_bin)

    # 3. Follow the newest managed release instead of pinning one version string.
    home = _profile_home().parent.parent
    raw_candidates = list((home / "superagent").glob("*/hermes/*/.hermes/bin/hermes"))
    valid_candidates = [p for p in raw_candidates if p.is_file()]
    if valid_candidates:
        valid_candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return str(valid_candidates[0])

    return None


def _engine_root() -> Path:
    env = os.environ.get("DEV_ENGINE_ROOT") or os.environ.get("PYTHONPATH", "").split(os.pathsep)[0]
    if env and Path(env).is_dir():
        return Path(env)
    return Path(__file__).resolve().parents[2]


def _read_job_envelope(stream) -> tuple[str, dict]:
    raw = stream.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        raise ValueError("job_input_budget")
    job = json.loads(raw)
    payload = job.get("payload") or {}
    goal = payload.get("goal")
    if not isinstance(goal, str) or not goal.strip() or len(goal) > MAX_GOAL:
        raise ValueError("invalid_goal")
    return goal.strip(), payload


def _read_job(stream) -> str:
    goal, _ = _read_job_envelope(stream)
    return goal


def _resolve_job_model_route(payload: dict | None = None) -> dict:
    engine = _engine_root()
    if str(engine) not in sys.path:
        sys.path.insert(0, str(engine))
    payload = payload or {}
    role = (
        os.environ.get("DEV_MODEL_ROLE")
        or payload.get("_colleague_role")
        or payload.get("role")
        or "dev_runner"
    )
    requested_model = (
        os.environ.get("DEV_MODEL_TIER")
        or payload.get("_model_tier")
        or os.environ.get("DEV_PI_MODEL")
        or ""
    )
    try:
        from agentcore.model_routing import resolve_colleague_model
        return resolve_colleague_model(str(role), str(requested_model))
    except Exception:
        return {
            "selected_model": str(requested_model or "flash"),
            "selected_reasoning_effort": "medium",
            "selected_tier": "quick",
            "role": str(role),
            "degraded": False,
            "degradation_reason": "",
        }


def _colleague_profile_home(role: str | None) -> Path | None:
    if not role or role in ("dev_runner", "default"):
        return None
    base = _profile_home()
    candidates = [
        base / "profiles" / role,
        base.parent / role if base.parent.name == "profiles" else None,
        Path.home() / ".hermes" / "profiles" / role,
    ]
    for c in candidates:
        if c and c.is_dir():
            return c
    return None


def _clean_env(route: dict | None = None) -> dict[str, str]:
    env = dict(os.environ)
    role = str(route.get("role") or "") if route else ""
    colleague_home = _colleague_profile_home(role)
    if colleague_home:
        env["HERMES_HOME"] = str(colleague_home)
        env["HERMES_SESSION_CHAT_ID"] = f"colleague-{role}-{os.getpid()}"
    else:
        env["HERMES_HOME"] = str(_profile_home())
        env["HERMES_SESSION_CHAT_ID"] = f"dev-worker-{os.getpid()}"
    env["HERMES_SESSION_SOURCE"] = "cli"
    # The backend keeps the full toolset; only human chat surfaces are thin.
    env["HERMES_SESSION_PLATFORM"] = "api_server"
    # Backend execution is unattended: no approval prompts may block a job.
    env["HERMES_YOLO_MODE"] = "1"
    # Tell durable_dispatch this turn is an executor, not a frontdesk: without
    # it the nested agent inherits the dispatch prompt and re-submits the work.
    env["SUPERAGENT_BACKEND_RUNNER"] = "1"
    if route:
        if route.get("selected_model"):
            env["SUPERAGENT_MODEL_TIER"] = str(route["selected_model"])
        if route.get("selected_reasoning_effort"):
            env["SUPERAGENT_REASONING_EFFORT"] = str(route["selected_reasoning_effort"])
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        env.pop(key, None)
    return env


def _workdir() -> Path:
    explicit = os.environ.get("DEV_WORKDIR")
    path = Path(explicit).expanduser() if explicit else Path.home() / "code" / "super-agent"
    return path if path.is_dir() else Path.home()


def run_hermes(goal: str, *, route: dict | None = None) -> dict:
    effective_goal = _decorate_goal(goal)
    budget = int(os.environ.get("DEV_BUDGET", "480"))
    max_turns = int(os.environ.get("DEV_MAX_TURNS", "40"))
    ssh_host = (os.environ.get("DEV_SSH_HOST") or "").strip()
    if ssh_host:
        if not _SSH_HOST_RE.fullmatch(ssh_host):
            return {"ok": False, "error": "invalid_ssh_host"}
        remote_bin = (os.environ.get("DEV_REMOTE_HERMES_BIN") or "hermes").strip()
        remote_home = (os.environ.get("DEV_REMOTE_PROFILE_HOME") or "").strip()
        remote_workdir = (os.environ.get("DEV_REMOTE_WORKDIR") or "").strip()
        role = str(route.get("role") or "") if route else ""
        has_colleague = bool(role and role not in ("dev_runner", "default"))
        colleague_id = f"colleague-{role}-{os.getpid()}" if has_colleague else f"dev-worker-{os.getpid()}"
        env_parts = [
            "HERMES_SESSION_SOURCE=cli",
            "HERMES_SESSION_PLATFORM=api_server",
            f"HERMES_SESSION_CHAT_ID={colleague_id}",
            "HERMES_YOLO_MODE=1",
            "SUPERAGENT_BACKEND_RUNNER=1",
        ]
        if route and route.get("selected_model"):
            env_parts.append("SUPERAGENT_MODEL_TIER=" + shlex.quote(str(route["selected_model"])))
        if has_colleague:
            if remote_home:
                env_parts.append("HERMES_HOME=" + shlex.quote(f"{remote_home}/profiles/{role}"))
            else:
                env_parts.append(f'HERMES_HOME="$HOME/.hermes/profiles/{role}"')
        elif remote_home:
            env_parts.append("HERMES_HOME=" + shlex.quote(remote_home))
        remote_cmd = " ".join(env_parts) + " " + " ".join([
            shlex.quote(remote_bin), "chat", "--query-file", "-", "--oneshot",
            "--run-budget", str(budget), "--max-turns", str(max_turns),
        ])
        if remote_workdir:
            remote_cmd = f"cd {shlex.quote(remote_workdir)} && {remote_cmd}"
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", ssh_host, remote_cmd]
        run_kwargs = {"input": effective_goal, "env": _clean_env(route)}
    else:
        binary = _hermes_bin()
        if not binary:
            return {"ok": False, "error": "hermes_unavailable"}
        cmd = [binary, "chat", "-q", effective_goal, "--oneshot",
               "--run-budget", str(budget), "--max-turns", str(max_turns)]
        run_kwargs = {"cwd": str(_workdir()), "env": _clean_env(route)}
    try:
        done = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=budget + 60, **run_kwargs)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "runner_timeout"}
    except Exception as exc:
        return {"ok": False, "error": "runner_spawn_failed: " + type(exc).__name__}
    full_stdout = done.stdout or ""
    out = full_stdout[-MAX_OUTPUT:]
    summary = _clean_hermes_output(full_stdout)[-MAX_OUTPUT:]
    err = (done.stderr or "")[-MAX_STDERR:]
    ok = done.returncode == 0 and bool((summary or out).strip())
    payload = {
        "ok": ok,
        "summary": summary or out.strip(),
        "output": out,
        "stderr": err,
        "returncode": done.returncode,
    }
    if not ok:
        payload["error"] = "turn_failed"
    return payload


def run_pi(goal: str, *, route: dict | None = None) -> dict:
    engine = _engine_root()
    if str(engine) not in sys.path:
        sys.path.insert(0, str(engine))
    try:
        from toolsets.pi_executor import (resolve_ccp_credentials, resolve_cli_path,
                                          run_pi, write_models_json)
        from toolsets.pi_executor.ccp_config import CCP_KEY_ENV_VAR
    except Exception as exc:
        return {"ok": False, "error": "pi_executor_unavailable: " + type(exc).__name__}

    resolved_route = route or _resolve_job_model_route()
    model = str(resolved_route.get("selected_model") or "flash")
    provider = os.environ.get("DEV_PI_PROVIDER", "ccp")
    timeout = int(os.environ.get("DEV_PI_TIMEOUT", os.environ.get("DEV_BUDGET", "300")))
    agent_dir = _profile_home() / "cache" / "scratch" / "dev-runner-pi"
    try:
        agent_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        credentials = resolve_ccp_credentials(alias=model)
        write_models_json(agent_dir, base_url=credentials.base_url, alias=credentials.alias)
        result = run_pi(
            goal, workdir=str(_workdir()), agent_dir=agent_dir,
            model=model, provider=provider, tools="read,write,edit,bash",
            timeout_s=timeout, env_extra={CCP_KEY_ENV_VAR: credentials.api_key},
            cli_path=os.environ.get("PI_EXECUTOR_CLI") or resolve_cli_path(None),
        )
    except Exception as exc:
        return {"ok": False, "error": "pi_run_failed: " + type(exc).__name__}
    text = (result.final_text or "")[-MAX_OUTPUT:]
    ok = bool(result.ok) and bool(text.strip())
    payload = {"ok": ok, "summary": text.strip(), "output": text, "stderr": "", "returncode": result.exit_code,
               "stop_reason": result.stop_reason, "tool_calls": result.tool_calls,
               "duration_ms": result.duration_ms, "model": model}
    if not ok:
        payload["error"] = result.error_message or result.stop_reason or "pi_failed"
    return payload


def _call_engine(fn, goal: str, route: dict | None = None) -> dict:
    import inspect
    try:
        sig = inspect.signature(fn)
        if any(p.kind == inspect.Parameter.VAR_KEYWORD or p.name == "route"
               for p in sig.parameters.values()):
            return fn(goal, route=route)
    except (TypeError, ValueError):
        pass
    return fn(goal)


def main() -> int:
    try:
        goal, job_payload = _read_job_envelope(sys.stdin)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": "invalid_job: " + type(exc).__name__}))
        return 1

    route = _resolve_job_model_route(job_payload)
    model_tier = str(route.get("selected_model") or "flash")
    route_degraded = bool(route.get("degraded"))
    route_reason = str(route.get("degradation_reason") or "")

    primary = _call_engine(run_hermes, goal, route=route)
    if primary.get("ok"):
        print(json.dumps({
            "ok": True,
            "engine": "hermes",
            "model_tier": model_tier,
            "degraded": route_degraded,
            "engine_degraded": False,
            "degradation_reason": route_reason,
            "result": primary,
        }, ensure_ascii=False))
        return 0

    fallback = _call_engine(run_pi, goal, route=route)
    primary_err = primary.get("error", "turn_failed")
    fallback_reason = f"primary_engine_fallback:hermes->{primary_err}"
    combined_reason = "; ".join(r for r in (route_reason, fallback_reason) if r)
    if fallback.get("ok"):
        print(json.dumps({
            "ok": True,
            "engine": "pi",
            "model_tier": model_tier,
            "degraded": True,
            "engine_degraded": True,
            "degradation_reason": combined_reason,
            "result": fallback,
            "primary_error": primary_err,
        }, ensure_ascii=False))
        return 0

    print(json.dumps({
        "ok": False,
        "engine": "none",
        "model_tier": model_tier,
        "degraded": True,
        "engine_degraded": True,
        "degradation_reason": combined_reason,
        "result": fallback,
        "primary_error": primary_err,
        "error": "both_engines_failed",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
