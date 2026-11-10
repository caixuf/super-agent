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
import threading
import time

MAX_INPUT = 131072
MAX_GOAL = 12000
MAX_OUTPUT = 32768
MAX_STDERR = 1500
_SSH_HOST_RE = re.compile(r"^[A-Za-z0-9._@-]{1,128}\Z")


def _compose_goal(goal: str, payload: dict | None) -> str:
    """Attach the requester's own words so a lossy frontdesk brief cannot hide the task."""
    payload = payload or {}
    user_request = payload.get("_user_request")
    conversation = payload.get("_conversation")
    sections = [goal]
    if isinstance(user_request, str) and user_request.strip() and user_request.strip() != goal.strip():
        sections.append("[用户原话（以此为准；与上面的任务描述冲突时按用户原话执行）]\n" + user_request.strip()[:4000])
    if isinstance(conversation, str) and conversation.strip():
        sections.append("[此前对话摘录（用于理解“这个/上面/继续”等指代）]\n" + conversation.strip()[:4000])
    if len(sections) == 1:
        return goal
    composed = "\n\n".join(sections)
    return composed[:MAX_GOAL]


def _decorate_goal(goal: str) -> str:
    duty = (os.environ.get("DEV_EXPERT_DUTY") or "").strip()
    target_host = (os.environ.get("DEV_TARGET_HOST") or "").strip()
    attempt_dir = (os.environ.get("SUPERAGENT_ATTEMPT_DIR") or "").strip()
    if not duty and not target_host:
        return goal
    lines = [
        "[Expert Context]",
        "- Mode: Backend executor (execute directly with terminal/file/MCP tools and return concrete evidence; do NOT call durable_submit).",
        "- Progressive Delivery: Follow conclusion-first progressive delivery (Phase 1: deliver core text analysis, Markdown report, and checkpoint output immediately; Phase 2: heavy diagram/browser rendering is an out-of-band enhancement. Never block, delay, or withhold Phase 1 text delivery due to rendering failures, delays, or timeouts).",
        "- Colleague Collaboration & Fault Tolerance: You can inspect the team roster (`colleague_list`) or spawn parallel subagents (`delegate_task`) when cross-role collaboration or parallel execution speeds up or recovers a task (do NOT call `durable_submit` or `colleague_assign` from a backend worker).",
    ]
    if attempt_dir:
        lines.append(f"- Deliverable Staging Directory: {attempt_dir} (copy any final deliverable files such as .html, .pdf, or cover .png into this directory so they are automatically attached to the chat completion)")
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


def _set_file_limit() -> None:
    try:
        import resource
        limit = int(os.environ.get("DEV_MAX_FILE", "536870912"))
        _, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
    except Exception:
        pass


def _hermes_bin() -> str | None:
    explicit = os.environ.get("DEV_HERMES_BIN")
    if explicit and Path(explicit).is_file():
        return explicit

    # 1. Follow the newest managed release first so worker turns never hit an
    # unmanaged or mid-update ~/.local/bin/hermes wrapper.
    home = _profile_home().parent.parent
    user_home = home.parent if home.name == ".hermes" else Path.home()
    raw_candidates = (
        list((user_home / "super_agent").glob("*/hermes-current/.hermes/bin/hermes"))
        + list((user_home / "super_agent").glob("*/hermes/*/.hermes/bin/hermes"))
        + list((home / "superagent").glob("*/hermes/*/.hermes/bin/hermes"))
    )
    valid_candidates = [p for p in raw_candidates if p.is_file()]
    if valid_candidates:
        valid_candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return str(valid_candidates[0])

    # 2. Search PATH
    on_path = shutil.which("hermes")
    if on_path and Path(on_path).is_file():
        return on_path

    # 3. Search user local bin
    local_bin = Path.home() / ".local" / "bin" / "hermes"
    if local_bin.is_file():
        return str(local_bin)

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
    if isinstance(job, dict):
        if "job" in job and "_job_id" not in payload:
            payload["_job_id"] = str(job["job"])
        if "attempt" in job and "_attempt_id" not in payload:
            payload["_attempt_id"] = str(job["attempt"])
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
    if os.environ.get("DEV_VERIFY_ON_STOP", "1").strip().lower() not in ("0", "false", "no", "off"):
        env.setdefault("HERMES_VERIFY_ON_STOP", "1")
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


_DELIVERABLE_EXTS = {
    ".html", ".htm", ".pdf", ".zip", ".csv", ".xlsx", ".docx",
    ".png", ".jpg", ".jpeg", ".webp", ".svg",
}
_INTERNAL_ATTEMPT_FILES = {
    "binding.json", "input.json", "progress.json", "progress.json.tmp",
    "stop.json", "manifest.json", "report.json", "checkpoint.json",
    "stdout", "stderr", "stdout.log", "stderr.log",
}
_ABS_FILE_RE = re.compile(r"(?<![A-Za-z0-9_.\-/])(/(?:home|tmp|data)(?:/[A-Za-z0-9_.\-]+)+\.[A-Za-z0-9]+)")


def _harvest_attempt_artifacts(summary: str, checkpoint_data: dict | None = None) -> list[str]:
    """Stage newly generated deliverable files into SUPERAGENT_ATTEMPT_DIR and return their paths."""
    attempt_env = (os.environ.get("SUPERAGENT_ATTEMPT_DIR") or "").strip()
    if not attempt_env:
        return []
    attempt_dir = Path(attempt_env)
    if not attempt_dir.is_dir() or attempt_dir.is_symlink():
        return []
    resolved_attempt = attempt_dir.resolve()
    staged: list[str] = []
    seen_names: set[str] = set()

    # 1. Existing deliverable files already placed directly inside SUPERAGENT_ATTEMPT_DIR
    try:
        for item in sorted(resolved_attempt.iterdir()):
            if (item.name.startswith(".") or item.name in _INTERNAL_ATTEMPT_FILES
                    or item.is_symlink() or not item.is_file()):
                continue
            ext = ".tar.gz" if item.name.lower().endswith(".tar.gz") else item.suffix.lower()
            if ext in _DELIVERABLE_EXTS or ext == ".tar.gz":
                if item.stat().st_size <= 16 * 1024 * 1024 and len(staged) < 8:
                    staged.append(str(item))
                    seen_names.add(item.name)
    except Exception:
        pass

    # 2. Deliverable files referenced in checkpoint_data or summary text
    candidates: list[str] = []
    if isinstance(checkpoint_data, dict) and isinstance(checkpoint_data.get("artifacts"), list):
        for p in checkpoint_data["artifacts"]:
            if isinstance(p, str) and p.startswith("/"):
                candidates.append(p)
    if isinstance(summary, str) and summary:
        for m in _ABS_FILE_RE.finditer(summary):
            candidates.append(m.group(1).rstrip(".,;:)`'\""))

    home_resolved = Path.home().resolve()
    tmp_resolved = Path("/tmp").resolve()
    now = time.time()

    for raw_path in candidates:
        if len(staged) >= 8:
            break
        try:
            src = Path(raw_path)
            if src.name.startswith(".") or src.name in _INTERNAL_ATTEMPT_FILES or src.is_symlink():
                continue
            if re.search(r"\.page-\d+\.png$", src.name, re.IGNORECASE):
                continue
            ext = ".tar.gz" if src.name.lower().endswith(".tar.gz") else src.suffix.lower()
            if ext not in _DELIVERABLE_EXTS and ext != ".tar.gz":
                continue
            resolved_src = src.resolve(strict=True)
            if not resolved_src.is_file():
                continue
            st = resolved_src.stat()
            if st.st_size == 0 or st.st_size > 16 * 1024 * 1024:
                continue
            if resolved_src.parent == resolved_attempt:
                if resolved_src.name not in seen_names:
                    staged.append(str(resolved_src))
                    seen_names.add(resolved_src.name)
                continue
            # Only stage fresh deliverable outputs created within safe report/temp roots
            in_safe_root = (
                resolved_src.is_relative_to(home_resolved / "reports")
                or resolved_src.is_relative_to(tmp_resolved)
                or resolved_src.is_relative_to(resolved_attempt)
            )
            if not in_safe_root or (now - st.st_mtime) > 7200:
                continue
            dest = resolved_attempt / resolved_src.name
            if dest.name not in seen_names:
                if not dest.exists():
                    shutil.copyfile(resolved_src, dest, follow_symlinks=False)
                staged.append(str(dest))
                seen_names.add(dest.name)
        except Exception:
            continue

    return staged[:8]


def _find_and_load_checkpoint() -> dict | None:
    engine = _engine_root()
    if str(engine) not in sys.path:
        sys.path.insert(0, str(engine))
    try:
        from agentcore.progressive_delivery import load_checkpoint
    except Exception:
        load_checkpoint = None
    candidates = []
    env_ckpt = os.environ.get("DEV_CHECKPOINT_PATH")
    if env_ckpt:
        candidates.append(Path(env_ckpt))
    candidates.append(_workdir() / "checkpoint.json")
    candidates.append(Path("checkpoint.json"))
    for path in candidates:
        try:
            if load_checkpoint is not None and path.name == "checkpoint.json" and path.parent.is_dir():
                loaded = load_checkpoint(path.parent)
                if isinstance(loaded, dict) and (loaded.get("summary") or loaded.get("phase")):
                    return loaded
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and (data.get("summary") or data.get("phase")):
                    return data
        except Exception:
            pass
    return None


def run_hermes(goal: str, *, route: dict | None = None) -> dict:
    effective_goal = _decorate_goal(goal)
    budget = int(os.environ.get("DEV_BUDGET", "3600"))
    max_turns = int(os.environ.get("DEV_MAX_TURNS", "1000"))
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
        if os.environ.get("DEV_VERIFY_ON_STOP", "1").strip().lower() not in ("0", "false", "no", "off"):
            env_parts.append("HERMES_VERIFY_ON_STOP=1")
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
        run_kwargs = {"cwd": str(_workdir()), "env": _clean_env(route), "preexec_fn": _set_file_limit}
    try:
        done = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=budget + 60, **run_kwargs)
    except subprocess.TimeoutExpired as exc:
        full_stdout = exc.stdout or ""
        cleaned = _clean_hermes_output(full_stdout) if isinstance(full_stdout, str) else ""
        summary = cleaned[:MAX_OUTPUT] if len(cleaned) > MAX_OUTPUT else cleaned
        checkpoint_data = _find_and_load_checkpoint()
        if not summary.strip() and checkpoint_data and checkpoint_data.get("summary"):
            summary = checkpoint_data["summary"]
        if summary.strip():
            # Progressive delivery: salvage Phase 1 text checkpoint even if later rendering steps timed out
            payload = {
                "ok": True,
                "summary": summary.strip(),
                "output": (full_stdout[:MAX_OUTPUT] if isinstance(full_stdout, str) else ""),
                "stderr": (exc.stderr or "")[-MAX_STDERR:] if isinstance(exc.stderr, str) else "",
                "returncode": -1,
                "degraded": True,
                "degradation_reason": "runner_timeout_checkpoint_salvaged",
                "phase": "phase1_text",
                "checkpoint_delivered": True,
            }
            if checkpoint_data:
                payload["checkpoint"] = checkpoint_data
            return payload
        return {"ok": False, "error": "runner_timeout"}
    except Exception as exc:
        return {"ok": False, "error": "runner_spawn_failed: " + type(exc).__name__}
    full_stdout = done.stdout or ""
    out = full_stdout[:MAX_OUTPUT] if len(full_stdout) > MAX_OUTPUT else full_stdout
    cleaned = _clean_hermes_output(full_stdout) if isinstance(full_stdout, str) else ""
    summary = cleaned[:MAX_OUTPUT] if len(cleaned) > MAX_OUTPUT else cleaned
    err = (done.stderr or "")[-MAX_STDERR:]
    ok = done.returncode == 0 and bool((summary or out).strip())

    checkpoint_data = _find_and_load_checkpoint()
    degraded = False
    degradation_reason = ""
    if not ok and checkpoint_data and checkpoint_data.get("summary"):
        summary = checkpoint_data["summary"]
        ok = True
        degraded = True
        degradation_reason = "phase1_checkpoint_recovered"

    payload = {
        "ok": ok,
        "summary": summary or out.strip(),
        "output": out,
        "stderr": err,
        "returncode": done.returncode,
    }
    artifacts = _harvest_attempt_artifacts(payload["summary"], checkpoint_data)
    if artifacts:
        payload["artifacts"] = artifacts
    if checkpoint_data:
        payload["checkpoint"] = checkpoint_data
        payload["checkpoint_delivered"] = True
    if degraded:
        payload["degraded"] = True
        payload["degradation_reason"] = degradation_reason
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
    raw_text = result.final_text or ""
    text = raw_text[:MAX_OUTPUT] if len(raw_text) > MAX_OUTPUT else raw_text
    ok = bool(result.ok) and bool(text.strip())
    payload = {"ok": ok, "summary": text.strip(), "output": text, "stderr": "", "returncode": result.exit_code,
               "stop_reason": result.stop_reason, "tool_calls": result.tool_calls,
               "duration_ms": result.duration_ms, "model": model}
    artifacts = _harvest_attempt_artifacts(payload["summary"], _find_and_load_checkpoint())
    if artifacts:
        payload["artifacts"] = artifacts
    if not ok:
        payload["error"] = result.error_message or result.stop_reason or "pi_failed"
    return payload


class ProgressMonitor:
    """Live execution progress monitor streaming tool steps and status into liveness.db and progress.json."""

    def __init__(
        self,
        job_id: str | None,
        attempt_dir: Path | None,
        *,
        log_file: Path | None = None,
        liveness_path: Path | None = None,
    ):
        self.job_id = job_id
        self.attempt_dir = attempt_dir
        self.log_file = log_file
        self.liveness_path = liveness_path
        self.ledger = None
        if self.job_id and self.liveness_path and Path(self.liveness_path).exists():
            try:
                engine = _engine_root()
                if str(engine) not in sys.path:
                    sys.path.insert(0, str(engine))
                from agentcore.liveness import LivenessLedger
                self.ledger = LivenessLedger(self.liveness_path)
            except Exception:
                pass
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.step = 0
        self.last_phase = "执行进程已启动"
        self.last_progress = 0.05
        self._log_offset = 0
        if self.log_file and self.log_file.is_file():
            try:
                self._log_offset = self.log_file.stat().st_size
            except OSError:
                self._log_offset = 0

    def _record_progress(
        self,
        phase: str,
        progress: float,
        *,
        step: int = 0,
        status: str = "running",
    ) -> None:
        now = time.time()
        if self.attempt_dir and self.attempt_dir.is_dir():
            try:
                pdata = {
                    "status": status,
                    "phase": phase,
                    "progress": round(progress, 3),
                    "step": step,
                    "updated_at": now,
                }
                tmp = self.attempt_dir / "progress.json.tmp"
                tmp.write_text(json.dumps(pdata, ensure_ascii=False), encoding="utf-8")
                tmp.replace(self.attempt_dir / "progress.json")
            except Exception:
                pass

        if self.ledger and self.job_id:
            try:
                done_pct = int(round(progress * 100))
                self.ledger.beat(self.job_id, phase=phase, progress=(done_pct, 100))
            except Exception:
                pass

    def start(self) -> None:
        self._record_progress(self.last_phase, self.last_progress, step=0)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        tool_done_re = re.compile(r"agent\.tool_executor:\s+tool\s+(\w+)\s+completed")
        tool_start_re = re.compile(r"agent\.tool_executor:\s+tool\s+(\w+)\s+(?:started|calling)")
        api_call_re = re.compile(r"agent\.conversation_loop:\s+API call #(\d+)")
        while not self._stop_event.is_set():
            if self._stop_event.wait(2.5):
                break
            if self.log_file and self.log_file.is_file():
                try:
                    size = self.log_file.stat().st_size
                    if size < self._log_offset:
                        self._log_offset = 0
                    if size > self._log_offset:
                        with self.log_file.open("r", encoding="utf-8", errors="replace") as f:
                            f.seek(self._log_offset)
                            chunk = f.read(262144)
                            self._log_offset = f.tell()

                        done_tools = tool_done_re.findall(chunk)
                        if done_tools:
                            self.step += len(done_tools)
                            last_tool = done_tools[-1]
                            self.last_phase = f"工具执行完成: {last_tool} (第{self.step}步)"
                            self.last_progress = min(0.92, 0.05 + 0.87 * (1.0 - (0.93 ** self.step)))
                        else:
                            starts = tool_start_re.findall(chunk)
                            if starts:
                                self.last_phase = f"正在执行工具: {starts[-1]}..."
                            else:
                                api_calls = api_call_re.findall(chunk)
                                if api_calls:
                                    self.last_phase = f"大模型交互中 (第{api_calls[-1]}轮)"
                except Exception:
                    pass

            self._record_progress(self.last_phase, self.last_progress, step=self.step)

    def stop(self, ok: bool = True) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)
        final_phase = "执行完成" if ok else "执行未完成"
        final_progress = 1.0 if ok else self.last_progress
        self._record_progress(
            final_phase,
            final_progress,
            step=self.step,
            status="completed" if ok else "failed",
        )


def _start_progress_monitor(payload: dict | None = None, route: dict | None = None) -> ProgressMonitor:
    payload = payload or {}
    job_id = (os.environ.get("SUPERAGENT_JOB_ID")
              or payload.get("_job_id")
              or payload.get("job")
              or payload.get("task_id"))
    attempt_dir_env = os.environ.get("SUPERAGENT_ATTEMPT_DIR")
    attempt_dir = Path(attempt_dir_env) if attempt_dir_env else Path.cwd()

    role = str(route.get("role") or "") if route else ""
    colleague_home = _colleague_profile_home(role)
    base_home = colleague_home if colleague_home else _profile_home()
    log_file = base_home / "logs" / "agent.log"

    liveness_db_env = os.environ.get("SUPERAGENT_LIVENESS_DB") or os.environ.get("AGENTCORE_LIVENESS_DB")
    liveness_path = None
    if liveness_db_env:
        liveness_path = Path(liveness_db_env)
    elif attempt_dir:
        cand1 = attempt_dir.parent.parent / "liveness.db"
        if cand1.is_file():
            liveness_path = cand1
        else:
            cand2 = Path.home() / ".local" / "state" / "agentcore" / "liveness.db"
            if cand2.is_file():
                liveness_path = cand2

    monitor = ProgressMonitor(
        job_id=job_id,
        attempt_dir=attempt_dir,
        log_file=log_file,
        liveness_path=liveness_path,
    )
    monitor.start()
    return monitor


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
    goal = _compose_goal(goal, job_payload)
    model_tier = str(route.get("selected_model") or "flash")
    route_degraded = bool(route.get("degraded"))
    route_reason = str(route.get("degradation_reason") or "")

    monitor = _start_progress_monitor(job_payload, route)
    try:
        primary = _call_engine(run_hermes, goal, route=route)
        if primary.get("ok"):
            monitor.stop(ok=True)
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
            monitor.stop(ok=True)
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

        # If both engines failed, salvage Phase 1 checkpoint from disk if one was persisted
        salvaged_checkpoint = _find_and_load_checkpoint()
        if salvaged_checkpoint and salvaged_checkpoint.get("summary"):
            monitor.stop(ok=True)
            print(json.dumps({
                "ok": True,
                "engine": "checkpoint_salvaged",
                "model_tier": model_tier,
                "degraded": True,
                "engine_degraded": True,
                "degradation_reason": "phase1_checkpoint_salvaged_after_engine_failure",
                "result": {
                    "ok": True,
                    "summary": salvaged_checkpoint["summary"],
                    "checkpoint": salvaged_checkpoint,
                    "phase": "phase1_text",
                    "checkpoint_delivered": True,
                    "degraded": True,
                    "degradation_reason": "phase1_checkpoint_salvaged_after_engine_failure",
                },
            }, ensure_ascii=False))
            return 0

        monitor.stop(ok=False)
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
    finally:
        monitor.stop(ok=False)


if __name__ == "__main__":
    raise SystemExit(main())
