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
import subprocess
import sys

MAX_INPUT = 131072
MAX_GOAL = 12000
MAX_OUTPUT = 8000
MAX_STDERR = 1500


def _profile_home() -> Path:
    env = os.environ.get("HERMES_HOME") or os.environ.get("DEV_PROFILE_HOME")
    return Path(env).expanduser() if env else Path.home() / ".hermes"


def _hermes_bin() -> str | None:
    explicit = os.environ.get("DEV_HERMES_BIN")
    if explicit:
        return explicit if Path(explicit).is_file() else None
    # Follow the newest managed release instead of pinning one version string.
    home = _profile_home().parent.parent
    candidates = sorted(
        (home / "superagent").glob("*/hermes/*/.hermes/bin/hermes"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    return str(candidates[0]) if candidates else None


def _engine_root() -> Path:
    env = os.environ.get("DEV_ENGINE_ROOT") or os.environ.get("PYTHONPATH", "").split(os.pathsep)[0]
    if env and Path(env).is_dir():
        return Path(env)
    return Path(__file__).resolve().parents[2]


def _read_job(stream) -> str:
    raw = stream.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        raise ValueError("job_input_budget")
    job = json.loads(raw)
    goal = (job.get("payload") or {}).get("goal")
    if not isinstance(goal, str) or not goal.strip() or len(goal) > MAX_GOAL:
        raise ValueError("invalid_goal")
    return goal.strip()


def _clean_env() -> dict[str, str]:
    env = dict(os.environ)
    env["HERMES_HOME"] = str(_profile_home())
    env["HERMES_SESSION_SOURCE"] = "cli"
    # The backend keeps the full toolset; only human chat surfaces are thin.
    env["HERMES_SESSION_PLATFORM"] = "api_server"
    env["HERMES_SESSION_CHAT_ID"] = "dev-worker"
    # Backend execution is unattended: no approval prompts may block a job.
    env["HERMES_YOLO_MODE"] = "1"
    # Tell durable_dispatch this turn is an executor, not a frontdesk: without
    # it the nested agent inherits the dispatch prompt and re-submits the work.
    env["SUPERAGENT_BACKEND_RUNNER"] = "1"
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        env.pop(key, None)
    return env


def _workdir() -> Path:
    explicit = os.environ.get("DEV_WORKDIR")
    path = Path(explicit).expanduser() if explicit else Path.home() / "code" / "super-agent"
    return path if path.is_dir() else Path.home()


def run_hermes(goal: str) -> dict:
    binary = _hermes_bin()
    if not binary:
        return {"ok": False, "error": "hermes_unavailable"}
    budget = int(os.environ.get("DEV_BUDGET", "240"))
    max_turns = int(os.environ.get("DEV_MAX_TURNS", "12"))
    cmd = [binary, "chat", "-q", goal, "--oneshot",
           "--run-budget", str(budget), "--max-turns", str(max_turns)]
    try:
        done = subprocess.run(cmd, cwd=str(_workdir()), env=_clean_env(),
                              capture_output=True, text=True, timeout=budget + 60)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "runner_timeout"}
    except Exception as exc:
        return {"ok": False, "error": "runner_spawn_failed: " + type(exc).__name__}
    out = (done.stdout or "")[-MAX_OUTPUT:]
    err = (done.stderr or "")[-MAX_STDERR:]
    ok = done.returncode == 0 and bool(out.strip())
    payload = {"ok": ok, "output": out, "stderr": err, "returncode": done.returncode}
    if not ok:
        payload["error"] = "turn_failed"
    return payload


def run_pi(goal: str) -> dict:
    engine = _engine_root()
    if str(engine) not in sys.path:
        sys.path.insert(0, str(engine))
    try:
        from toolsets.pi_executor import (resolve_ccp_credentials, resolve_cli_path,
                                          run_pi, write_models_json)
        from toolsets.pi_executor.ccp_config import CCP_KEY_ENV_VAR
    except Exception as exc:
        return {"ok": False, "error": "pi_executor_unavailable: " + type(exc).__name__}

    model = os.environ.get("DEV_PI_MODEL", "flash")
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
    payload = {"ok": ok, "output": text, "stderr": "", "returncode": result.exit_code,
               "stop_reason": result.stop_reason, "tool_calls": result.tool_calls,
               "duration_ms": result.duration_ms}
    if not ok:
        payload["error"] = result.error_message or result.stop_reason or "pi_failed"
    return payload


def main() -> int:
    try:
        goal = _read_job(sys.stdin)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": "invalid_job: " + type(exc).__name__}))
        return 1

    primary = run_hermes(goal)
    if primary.get("ok"):
        print(json.dumps({"ok": True, "engine": "hermes", "result": primary}, ensure_ascii=False))
        return 0

    fallback = run_pi(goal)
    if fallback.get("ok"):
        print(json.dumps({"ok": True, "engine": "pi", "result": fallback,
                          "primary_error": primary.get("error", "turn_failed")},
                         ensure_ascii=False))
        return 0

    print(json.dumps({"ok": False, "engine": "none", "result": fallback,
                      "primary_error": primary.get("error", "turn_failed"),
                      "error": "both_engines_failed"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
