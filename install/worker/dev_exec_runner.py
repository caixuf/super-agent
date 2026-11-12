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
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any

MAX_INPUT = 131072
MAX_GOAL = 12000
MAX_OUTPUT = 32768
MAX_STDERR = 1500
EX_TEMPFAIL = 75
_SSH_HOST_RE = re.compile(r"^[A-Za-z0-9._@-]{1,128}\Z")
_SHUTDOWN_SIGNAL = 0

SANITISED_EMPTY_RESPONSES: frozenset[str] = frozenset({
    "(empty)",
    "[System: Empty message content sanitised to satisfy protocol]",
})


class _InterruptedShutdownError(RuntimeError):
    """Raised when SIGTERM/SIGINT interrupts worker execution so fallback engines do not spawn."""


def _handle_shutdown_signal(signum: int, _frame: Any) -> None:
    global _SHUTDOWN_SIGNAL
    _SHUTDOWN_SIGNAL = int(signum)


def _recall_skill_guidance(goal: str, payload: dict | None = None) -> tuple[str, list[str]]:
    """Query SkillForge for matching procedural skills to mount into the task context."""
    clean_q = (goal or "").strip()
    if len(clean_q) < 8:
        return "", []
    # Avoid injecting skills into minimal synthetic test words unless SkillForge dir is explicitly configured
    explicit_forge = (os.environ.get("SUPERAGENT_SKILL_FORGE_DIR") or "").strip()
    if not explicit_forge and len(clean_q.split()) <= 3 and clean_q.isascii():
        return "", []
    try:
        engine = _engine_root()
        if str(engine) not in sys.path:
            sys.path.insert(0, str(engine))
        from agentcore.skill_forge import SkillForge
        forge = SkillForge()
        domain = str((payload or {}).get("domain") or "").strip() or None
        skills = forge.recall_skills(clean_q, domain=domain, top_k=2)
        if not skills:
            return "", []
        guidance = forge.render_guidance(clean_q, domain=domain, top_k=2)
        return guidance, [s.skill_id for s in skills]
    except Exception:
        return "", []


def _persist_attempt_input_context(
    raw_goal: str,
    composed_goal: str,
    payload: dict | None,
    mounted_skills: list[str],
) -> None:
    """Persist composed goal and mounted procedural skills into SUPERAGENT_ATTEMPT_DIR/input.json."""
    attempt_env = (os.environ.get("SUPERAGENT_ATTEMPT_DIR") or "").strip()
    if not attempt_env:
        return
    attempt_dir = Path(attempt_env)
    if not attempt_dir.is_dir() or attempt_dir.is_symlink():
        return
    try:
        input_path = attempt_dir / "input.json"
        existing: dict = {}
        if input_path.is_file():
            try:
                loaded = json.loads(input_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            except Exception:
                existing = {}
        existing.setdefault("goal", raw_goal)
        existing["composed_goal"] = composed_goal
        existing["mounted_skills"] = list(mounted_skills)
        if isinstance(payload, dict) and payload.get("_job_id"):
            existing.setdefault("job_id", payload["_job_id"])
        tmp_path = attempt_dir / "input.json.tmp"
        tmp_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, input_path)
    except Exception:
        pass


def _record_and_distill_case(
    raw_goal: str,
    payload: dict | None,
    route: dict | None,
    result: dict,
    *,
    degraded: bool = False,
) -> list[str]:
    """Record completed execution trace as an AgentCase and auto-distill procedural skills."""
    clean_goal = (raw_goal or "").strip()
    explicit_forge = (os.environ.get("SUPERAGENT_SKILL_FORGE_DIR") or "").strip()
    if not explicit_forge and len(clean_goal) < 10:
        return []
    try:
        engine = _engine_root()
        if str(engine) not in sys.path:
            sys.path.insert(0, str(engine))
        from agentcore.skill_forge import AgentCase, SkillForge
        payload = payload or {}
        route = route or {}
        job_id = (
            os.environ.get("SUPERAGENT_JOB_ID")
            or payload.get("_job_id")
            or payload.get("job")
            or f"exec_{int(time.time())}"
        )
        attempt_id = (
            os.environ.get("SUPERAGENT_ATTEMPT_ID")
            or payload.get("_attempt_id")
            or payload.get("attempt")
            or "0"
        )
        role = str(route.get("role") or payload.get("_colleague_role") or payload.get("role") or "dev_runner")
        domain = str(payload.get("domain") or os.environ.get("SUPERAGENT_PROFILE") or "default").strip() or "default"
        summary = str(result.get("summary") or result.get("output") or "").strip()
        if not summary:
            return []
        artifacts = [str(p) for p in (result.get("artifacts") or []) if p]
        forge = SkillForge()
        case = AgentCase(
            case_id=f"case_{job_id}_{attempt_id}",
            task_intent=raw_goal[:240],
            approach=summary[:600],
            quality_score=95.0 if not degraded else 85.0,
            domain=domain,
            role=role,
            verified_files=artifacts,
            approved=False,
        )
        forge.record_case(case)
        distilled = forge.auto_distill_from_cases(domain=domain, role=role)
        return [s.skill_id for s in distilled]
    except Exception:
        return []


def _compose_goal(goal: str, payload: dict | None) -> str:
    """Attach the requester's own words and recalled ProceduralSkills so context is complete."""
    payload = payload or {}
    user_request = payload.get("_user_request")
    conversation = payload.get("_conversation")
    sections = [goal]
    if isinstance(user_request, str) and user_request.strip() and user_request.strip() != goal.strip():
        sections.append("[用户原话（以此为准；与上面的任务描述冲突时按用户原话执行）]\n" + user_request.strip()[:4000])
    if isinstance(conversation, str) and conversation.strip():
        sections.append("[此前对话摘录（用于理解“这个/上面/继续”等指代）]\n" + conversation.strip()[:4000])

    skill_guidance, mounted_skills = _recall_skill_guidance(goal, payload)
    if skill_guidance and "经过工程检验的先例技能" not in goal:
        sections.append(skill_guidance[:2400])

    if len(sections) == 1:
        _persist_attempt_input_context(goal, goal, payload, mounted_skills)
        return goal
    composed = "\n\n".join(sections)[:MAX_GOAL]
    _persist_attempt_input_context(goal, composed, payload, mounted_skills)
    return composed


def _decorate_goal(goal: str) -> str:
    duty = (os.environ.get("DEV_EXPERT_DUTY") or "").strip()
    target_host = (os.environ.get("DEV_TARGET_HOST") or "").strip()
    attempt_dir = (os.environ.get("SUPERAGENT_ATTEMPT_DIR") or "").strip()
    explicit_forge = (os.environ.get("SUPERAGENT_SKILL_FORGE_DIR") or "").strip()
    if not duty and not target_host and not explicit_forge:
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
    if "经过工程检验的先例技能" not in goal:
        skill_guidance, mounted_skills = _recall_skill_guidance(goal)
        if skill_guidance:
            lines.append("")
            lines.append(skill_guidance[:2400])
            _persist_attempt_input_context(goal, "\n".join(lines + ["", goal])[:MAX_GOAL], None, mounted_skills)
    lines.append("")
    lines.append(goal)
    return "\n".join(lines)[:MAX_GOAL]


def _clean_hermes_output(raw: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw or "")
    text = re.sub(
        r"^(?:\s*(?:警告[：:]\s*未知的工具集[：:]|Warning:\s*unknown toolset:)[^\n]*(?:\n|$))+",
        "",
        text,
    )
    parts = re.split(r"(?:^|\n)[─╭]*\s*[☤⚕]\s*Hermes[^\n]*\n", text)
    if len(parts) > 1:
        text = parts[-1]
    text = re.sub(
        r"^(?:\s*(?:警告[：:]\s*未知的工具集[：:]|Warning:\s*unknown toolset:)[^\n]*(?:\n|$))+",
        "",
        text,
    )
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


def _resolve_job_model_route(payload: dict | None = None) -> dict:
    engine = _engine_root()
    if str(engine) not in sys.path:
        sys.path.insert(0, str(engine))
    payload = payload or {}
    role = (
        payload.get("_colleague_role")
        or os.environ.get("DEV_MODEL_ROLE")
        or payload.get("role")
        or "dev_runner"
    )
    requested_model = (
        payload.get("_model_tier")
        or payload.get("model_tier")
        or os.environ.get("DEV_MODEL_TIER")
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
    ".html", ".htm", ".pdf", ".zip", ".tar.gz", ".csv", ".xlsx", ".docx",
    ".png", ".jpg", ".jpeg", ".webp", ".svg",
    ".md", ".markdown", ".txt", ".json", ".log",
}
_INTERNAL_ATTEMPT_FILES = {
    "binding.json", "input.json", "input.json.tmp", "progress.json", "progress.json.tmp",
    "stop.json", "manifest.json", "report.json", "checkpoint.json",
    "state_checkpoints.db", "state_checkpoints.db-wal", "state_checkpoints.db-shm",
    "containment_audit.json", "hermes_usage.json",
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

    # 3. Auto-materialize long text reports as artifact files if no markdown artifact exists
    if isinstance(summary, str) and len(summary.strip()) > 1500 and len(staged) < 8:
        has_doc = any(p.lower().endswith((".md", ".markdown", ".html", ".pdf", ".docx")) for p in staged)
        if not has_doc:
            report_file = resolved_attempt / "任务执行报告.md"
            if not report_file.exists():
                try:
                    report_file.write_text(summary.strip(), encoding="utf-8")
                    staged.append(str(report_file))
                    seen_names.add(report_file.name)
                except Exception:
                    pass

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


def _wrap_with_bwrap_sandbox(
    cmd: list[str],
    *,
    route: dict | None = None,
    env: dict[str, str] | None = None,
) -> tuple[list[str], dict | None]:
    """Wrap local worker execution in AttemptBoundSandboxAdapter (bwrap) when attempt binding is active."""
    toggle = (os.environ.get("SUPERAGENT_ENABLE_BWRAP") or "").strip().lower()
    if toggle in ("0", "false", "no", "off"):
        return cmd, None
    binding_path_raw = (os.environ.get("SUPERAGENT_ATTEMPT_BINDING_PATH") or "").strip()
    if not binding_path_raw and toggle not in ("1", "true", "yes", "on"):
        return cmd, None

    engine = _engine_root()
    if str(engine) not in sys.path:
        sys.path.insert(0, str(engine))
    try:
        from agentcore.colleague_containment import (
            AttemptBoundSandboxAdapter,
            is_forbidden_host_path,
            is_role_sandbox_exempt,
        )
        from agentcore.colleague_dev import DEV_ROLES
    except Exception:
        return cmd, None

    raw_role = str((route or {}).get("role") or os.environ.get("DEV_ROLE") or "cases").strip().lower()
    if is_role_sandbox_exempt(raw_role):
        return cmd, {
            "backend": "host",
            "role": raw_role,
            "exempt": True,
            "reason": "privileged_role_exemption",
            "binding": {"ok": True, "mode": "host_privileged"},
            "audit_log": [{
                "timestamp": time.time(),
                "role": raw_role,
                "action": "host_privileged_execution",
                "backend": "host",
                "reason": "privileged_role_exemption",
            }],
        }

    if not AttemptBoundSandboxAdapter.is_bwrap_functional():
        return cmd, None

    wdir = _workdir()
    if is_forbidden_host_path(wdir)[0]:
        return cmd, None

    attempt_env = (os.environ.get("SUPERAGENT_ATTEMPT_DIR") or "").strip()
    attempt_dir = Path(attempt_env) if attempt_env and Path(attempt_env).is_dir() else None
    adapter_role = raw_role if raw_role in DEV_ROLES else "cases"

    try:
        if binding_path_raw and Path(binding_path_raw).is_file():
            adapter = AttemptBoundSandboxAdapter.from_environment(
                allowed_workdir=wdir,
                role=adapter_role,
                network_policy="allowed",
            )
        else:
            adapter = AttemptBoundSandboxAdapter.create_host_bound(
                job=os.environ.get("SUPERAGENT_JOB_ID") or "standalone_job",
                attempt=os.environ.get("SUPERAGENT_ATTEMPT_ID") or "1",
                allowed_workdir=wdir,
                role=adapter_role,
                network_policy="allowed",
                attempt_dir=attempt_dir,
            )
        binding_check = adapter.verify_attempt_binding(
            expected_job=adapter.job,
            expected_attempt=adapter.attempt,
            expected_nonce=adapter.nonce,
            expected_token=adapter.token,
        )
        if not binding_check.get("ok"):
            return cmd, None

        reports_dir = Path.home() / "reports"
        try:
            reports_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        writable_paths: list[Path] = [reports_dir]

        runtime_home = (
            Path(env["HERMES_HOME"]).expanduser()
            if env and env.get("HERMES_HOME")
            else _profile_home()
        )
        pm_writable_paths: list[Path] = []
        hermes_shared_runtime = Path.home() / ".hermes" / "runtime"
        try:
            hermes_shared_runtime.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if hermes_shared_runtime.is_dir():
            pm_writable_paths.append(hermes_shared_runtime)
        installs_dir = Path.home() / ".hermes" / "installs"
        if installs_dir.is_dir():
            for inst_dir in sorted(installs_dir.iterdir()):
                if not inst_dir.is_dir():
                    continue
                install_lock = inst_dir / ".install.lock"
                try:
                    install_lock.touch(exist_ok=True)
                except OSError:
                    pass
                if install_lock.is_file():
                    pm_writable_paths.append(install_lock)
                pm_runtime_dir = inst_dir / "pm-runtime"
                if pm_runtime_dir.is_dir():
                    prepare_lock = pm_runtime_dir / ".prepare.lock"
                    try:
                        prepare_lock.touch(exist_ok=True)
                    except OSError:
                        pass
                    if prepare_lock.is_file():
                        pm_writable_paths.append(prepare_lock)
                    gens_dir = pm_runtime_dir / "generations"
                    if gens_dir.is_dir():
                        for gen_dir in sorted(gens_dir.iterdir()):
                            if not gen_dir.is_dir():
                                continue
                            gen_leases = gen_dir / ".leases"
                            try:
                                gen_leases.mkdir(exist_ok=True)
                            except OSError:
                                pass
                            if gen_leases.is_dir():
                                pm_writable_paths.append(gen_leases)
                envs_dir = inst_dir / "environments"
                if envs_dir.is_dir():
                    for env_dir in sorted(envs_dir.iterdir()):
                        if not env_dir.is_dir():
                            continue
                        leases_dir = env_dir / ".leases"
                        try:
                            leases_dir.mkdir(exist_ok=True)
                        except OSError:
                            pass
                        if leases_dir.is_dir():
                            pm_writable_paths.append(leases_dir)

        managed_state_paths: list[Path] = []
        candidate_state_roots: list[Path] = []
        if attempt_dir is not None and attempt_dir.parent.name == "attempts":
            candidate_state_roots.append(attempt_dir.parent.parent.parent)
        super_agent_base = Path.home() / "super_agent"
        if super_agent_base.is_dir():
            candidate_state_roots.extend(sorted(super_agent_base.glob("*/state")))
        candidate_state_roots.append(Path.home() / ".local" / "state" / "agentcore")
        seen_state_roots: set[str] = set()
        for st_root in candidate_state_roots:
            try:
                if not st_root.is_dir():
                    continue
                st_key = str(st_root.resolve(strict=True))
            except OSError:
                continue
            if st_key in seen_state_roots:
                continue
            seen_state_roots.add(st_key)
            for sub_name in ("memory-spool", "recall-diagnostics", "personal-memory-candidate"):
                sub_dir = st_root / sub_name
                try:
                    sub_dir.mkdir(exist_ok=True)
                except OSError:
                    pass
                if sub_dir.is_dir():
                    managed_state_paths.append(sub_dir)

        wrapped_cmd = adapter.build_worker_bwrap_argv(
            cmd,
            wdir,
            attempt_dir=attempt_dir,
            writable_paths=writable_paths,
            hermes_runtime_home=runtime_home,
            hermes_pm_writable_paths=pm_writable_paths,
            managed_state_paths=managed_state_paths,
        )
        containment_meta = {
            "backend": "bwrap",
            "binding": binding_check,
            "bwrap_argv": wrapped_cmd,
            "audit_log": adapter.audit_log,
        }
        if attempt_dir is not None:
            try:
                audit_file = attempt_dir / "containment_audit.json"
                audit_file.write_text(
                    json.dumps(containment_meta, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception:
                pass
        return wrapped_cmd, containment_meta
    except Exception:
        return cmd, None


def _build_local_hermes_cmd(
    binary: str,
    *,
    budget: int,
    max_turns: int,
    route: dict | None = None,
    env: dict[str, str] | None = None,
) -> list[str]:
    """Resolve an in-process sandboxed Hermes command when `binary` is a managed launcher.

    Upstream `hermes chat --oneshot` and `hermes -z` dial the shared profile gateway
    over WebSocket (`tui_gateway.ws`), which bypasses `bwrap` containment and fails
    with `rc=1` whenever the gateway daemon is restarting or absent on colleague
    profiles. Managed Hermes launchers expose `--print-runtime-command`, allowing the
    worker to boot `hermes_bootstrap` directly inside `bwrap` and run `AIAgent`
    in-process via `hermes_inproc_oneshot.py`.
    """
    fallback_cmd = [
        binary, "chat", "--query-file", "-", "--oneshot",
        "--run-budget", str(budget), "--max-turns", str(max_turns),
    ]
    if (os.environ.get("DEV_HERMES_CLI_MODE") or "").strip().lower() in ("1", "true", "chat"):
        return fallback_cmd
    inproc_script = Path(__file__).with_name("hermes_inproc_oneshot.py")
    if not inproc_script.is_file():
        return fallback_cmd
    try:
        bin_path = Path(binary)
        if not bin_path.is_file() or bin_path.stat().st_size > 131072:
            return fallback_cmd
        launcher_head = bin_path.read_bytes()[:65536]
        if b"--print-runtime-command" not in launcher_head:
            return fallback_cmd
        probe = subprocess.Popen(
            [binary, "--print-runtime-command"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        stdout_text, _ = probe.communicate(timeout=10)
        if probe.returncode != 0 or not stdout_text.strip():
            return fallback_cmd
        runtime_argv = json.loads(stdout_text.strip())
        if (
            not isinstance(runtime_argv, list)
            or len(runtime_argv) < 4
            or runtime_argv[2] != "-c"
            or not isinstance(runtime_argv[3], str)
        ):
            return fallback_cmd
        target_call = "runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)"
        if target_call not in runtime_argv[3]:
            return fallback_cmd
        script_literal = repr(str(inproc_script.resolve()))
        patched_bootstrap = runtime_argv[3].replace(
            target_call,
            f"runpy.run_path({script_literal}, run_name='__main__')",
            1,
        )
        cmd = [str(runtime_argv[0]), * [str(x) for x in runtime_argv[1:3]], patched_bootstrap]
        cmd.extend([
            "--query-file", "-",
            "--run-budget", str(budget),
            "--max-turns", str(max_turns),
        ])
        if route:
            if route.get("selected_model"):
                cmd.extend(["--model", str(route["selected_model"])])
            if route.get("selected_reasoning_effort"):
                cmd.extend(["--reasoning", str(route["selected_reasoning_effort"])])
        attempt_env = (os.environ.get("SUPERAGENT_ATTEMPT_DIR") or "").strip()
        if attempt_env and Path(attempt_env).is_dir():
            cmd.extend(["--usage-file", str(Path(attempt_env) / "hermes_usage.json")])
        return cmd
    except Exception:
        return fallback_cmd


def run_hermes(goal: str, *, route: dict | None = None) -> dict:
    effective_goal = _decorate_goal(goal)
    budget = int(os.environ.get("DEV_BUDGET", "3600"))
    max_turns = int(os.environ.get("DEV_MAX_TURNS", "1000"))
    ssh_host = (os.environ.get("DEV_SSH_HOST") or "").strip()
    containment_meta = None
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
        cleaned_env = _clean_env(route)
        cmd = _build_local_hermes_cmd(
            binary,
            budget=budget,
            max_turns=max_turns,
            route=route,
            env=cleaned_env,
        )
        cmd, containment_meta = _wrap_with_bwrap_sandbox(cmd, route=route, env=cleaned_env)
        run_kwargs = {"cwd": str(_workdir()), "env": cleaned_env, "input": effective_goal, "preexec_fn": _set_file_limit}
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
    if _SHUTDOWN_SIGNAL != 0 or done.returncode in (-15, 143, -2, 130):
        return {
            "ok": False,
            "interrupted": True,
            "error": "interrupted_by_shutdown",
            "returncode": done.returncode,
            "stderr": err,
        }
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
    if containment_meta is not None:
        payload["containment"] = {
            "backend": containment_meta.get("backend", "bwrap"),
            "binding_ok": bool((containment_meta.get("binding") or {}).get("ok")),
            "audit_entries": len(containment_meta.get("audit_log") or []),
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


def run_pi(goal: str, *, route: dict | None = None, on_event: Any | None = None) -> dict:
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
            on_event=on_event,
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

    def handle_pi_event(self, ev: dict[str, Any]) -> None:
        """Process fine-grained events from Engine 2 (Pi) to update progress.json in real time."""
        etype = ev.get("type")
        if etype == "tool_execution_start":
            tool_name = ev.get("toolName") or "tool"
            self.last_phase = f"正在执行工具: {tool_name}..."
            self._record_progress(self.last_phase, self.last_progress, step=self.step)
        elif etype == "tool_execution_end":
            tool_name = ev.get("toolName") or "tool"
            self.step += 1
            self.last_phase = f"工具执行完成: {tool_name} (第{self.step}步)"
            self.last_progress = min(0.92, 0.05 + 0.87 * (1.0 - (0.93 ** self.step)))
            self._record_progress(self.last_phase, self.last_progress, step=self.step)
        elif etype == "message_end":
            self.step += 1
            self.last_phase = f"大模型交互完成 (第{self.step}步)"
            self.last_progress = min(0.92, 0.05 + 0.87 * (1.0 - (0.93 ** self.step)))
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


def _call_engine(fn, goal: str, route: dict | None = None, on_event: Any | None = None) -> dict:
    import inspect
    try:
        sig = inspect.signature(fn)
        kwargs = {}
        if any(p.kind == inspect.Parameter.VAR_KEYWORD or p.name == "route"
               for p in sig.parameters.values()):
            kwargs["route"] = route
        if any(p.kind == inspect.Parameter.VAR_KEYWORD or p.name == "on_event"
               for p in sig.parameters.values()) and on_event is not None:
            kwargs["on_event"] = on_event
        return fn(goal, **kwargs)
    except (TypeError, ValueError):
        pass
    return fn(goal)


def main() -> int:
    global _SHUTDOWN_SIGNAL
    _SHUTDOWN_SIGNAL = 0
    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, _handle_shutdown_signal)
        except (ValueError, OSError):
            pass

    try:
        raw_goal, job_payload = _read_job_envelope(sys.stdin)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": "invalid_job: " + type(exc).__name__}))
        return 1

    engine = _engine_root()
    if str(engine) not in sys.path:
        sys.path.insert(0, str(engine))

    from agentcore.state_graph import (
        END,
        MemoryCheckpointer,
        NodeExecutionError,
        SqliteCheckpointer,
        StateGraph,
    )

    task_id = str(
        os.environ.get("SUPERAGENT_JOB_ID")
        or job_payload.get("_job_id")
        or job_payload.get("job")
        or job_payload.get("task_id")
        or f"exec_{os.getpid()}_{int(time.time())}"
    )
    attempt_env = (os.environ.get("SUPERAGENT_ATTEMPT_DIR") or "").strip()
    attempt_dir = Path(attempt_env) if attempt_env and Path(attempt_env).is_dir() else None
    attempt_ckpt_file = attempt_dir / "state_checkpoints.db" if attempt_dir is not None else None
    job_ckpt_file: Path | None = None
    if attempt_dir is not None:
        if attempt_dir.parent.name == "attempts":
            safe_task_key = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id)[:96] or "job"
            job_ckpt_file = attempt_dir.parent.parent / "checkpoints" / f"{safe_task_key}.db"
        else:
            job_ckpt_file = attempt_ckpt_file
        active_ckpt: MemoryCheckpointer | SqliteCheckpointer = SqliteCheckpointer(job_ckpt_file)
    else:
        active_ckpt = MemoryCheckpointer()

    def _sync_attempt_ckpt_snapshot() -> None:
        if (
            job_ckpt_file is None
            or attempt_ckpt_file is None
            or job_ckpt_file == attempt_ckpt_file
            or not job_ckpt_file.is_file()
        ):
            return
        try:
            with sqlite3.connect(job_ckpt_file, timeout=10.0) as src_conn:
                with sqlite3.connect(attempt_ckpt_file, timeout=10.0) as dst_conn:
                    src_conn.backup(dst_conn)
        except Exception:
            try:
                shutil.copy2(job_ckpt_file, attempt_ckpt_file)
            except Exception:
                pass

    explicit_resume = bool(job_payload.get("resume") or os.environ.get("SUPERAGENT_RESUME_CHECKPOINT") == "1")
    existing_ckpt = active_ckpt.get(task_id)
    if existing_ckpt is not None and existing_ckpt.node == "harvest_and_distill" and not explicit_resume:
        task_id = f"{task_id}_{job_payload.get('_attempt_id') or int(time.time())}"

    route = _resolve_job_model_route(job_payload)
    monitor = _start_progress_monitor(job_payload, route)

    def _node_prepare_context(state: dict) -> dict:
        composed = _compose_goal(raw_goal, job_payload)
        return {
            "composed_goal": composed,
            "model_tier": str(route.get("selected_model") or "flash"),
            "route_degraded": bool(route.get("degraded")),
            "route_reason": str(route.get("degradation_reason") or ""),
        }

    def _node_execute_engine(state: dict) -> dict:
        composed = str(state.get("composed_goal") or raw_goal)
        m_tier = str(state.get("model_tier") or route.get("selected_model") or "flash")
        r_degraded = bool(state.get("route_degraded"))
        r_reason = str(state.get("route_reason") or "")

        if _SHUTDOWN_SIGNAL != 0:
            raise _InterruptedShutdownError("interrupted_by_shutdown")

        primary = _call_engine(run_hermes, composed, route=route)
        if primary.get("interrupted") or _SHUTDOWN_SIGNAL != 0:
            raise _InterruptedShutdownError(str(primary.get("error") or "interrupted_by_shutdown"))

        if primary.get("ok"):
            return {
                "ok": True,
                "engine": "hermes",
                "result": primary,
                "envelope": {
                    "ok": True,
                    "engine": "hermes",
                    "model_tier": m_tier,
                    "degraded": r_degraded,
                    "engine_degraded": False,
                    "degradation_reason": r_reason,
                    "result": primary,
                },
            }

        fallback = _call_engine(run_pi, composed, route=route, on_event=monitor.handle_pi_event)
        primary_err = primary.get("error", "turn_failed")
        primary_stderr = str(primary.get("stderr") or "").strip()[-MAX_STDERR:]
        primary_rc = primary.get("returncode")
        fallback_reason = f"primary_engine_fallback:hermes->{primary_err}"
        combined_reason = "; ".join(r for r in (r_reason, fallback_reason) if r)
        if fallback.get("ok"):
            fb_env: dict[str, Any] = {
                "ok": True,
                "engine": "pi",
                "model_tier": m_tier,
                "degraded": True,
                "engine_degraded": True,
                "degradation_reason": combined_reason,
                "result": fallback,
                "primary_error": primary_err,
            }
            if primary_stderr:
                fb_env["primary_stderr"] = primary_stderr
            if primary_rc is not None:
                fb_env["primary_returncode"] = primary_rc
            return {
                "ok": True,
                "engine": "pi",
                "result": fallback,
                "envelope": fb_env,
            }

        salvaged_checkpoint = _find_and_load_checkpoint()
        if salvaged_checkpoint and salvaged_checkpoint.get("summary"):
            salvaged_res = {
                "ok": True,
                "summary": salvaged_checkpoint["summary"],
                "checkpoint": salvaged_checkpoint,
                "phase": "phase1_text",
                "checkpoint_delivered": True,
                "degraded": True,
                "degradation_reason": "phase1_checkpoint_salvaged_after_engine_failure",
            }
            return {
                "ok": True,
                "engine": "checkpoint_salvaged",
                "result": salvaged_res,
                "envelope": {
                    "ok": True,
                    "engine": "checkpoint_salvaged",
                    "model_tier": m_tier,
                    "degraded": True,
                    "engine_degraded": True,
                    "degradation_reason": "phase1_checkpoint_salvaged_after_engine_failure",
                    "result": salvaged_res,
                },
            }

        fail_env: dict[str, Any] = {
            "ok": False,
            "engine": "none",
            "model_tier": m_tier,
            "degraded": True,
            "engine_degraded": True,
            "degradation_reason": combined_reason,
            "result": fallback,
            "primary_error": primary_err,
            "error": "both_engines_failed",
        }
        if primary_stderr:
            fail_env["primary_stderr"] = primary_stderr
        if primary_rc is not None:
            fail_env["primary_returncode"] = primary_rc
        return {
            "ok": False,
            "engine": "none",
            "result": fallback,
            "envelope": fail_env,
        }

    def _node_harvest_and_distill(state: dict) -> dict:
        env_out = dict(state.get("envelope") or {})
        res_out = dict(state.get("result") or {})
        is_ok = bool(state.get("ok"))
        distilled_ids: list[str] = []
        if is_ok:
            distilled_ids = _record_and_distill_case(
                raw_goal,
                job_payload,
                route,
                res_out,
                degraded=bool(env_out.get("degraded")),
            )
        if state.get("_resumed_from_node"):
            env_out["resumed_from_node"] = state["_resumed_from_node"]
        return {
            "envelope": env_out,
            "distilled_skill_ids": distilled_ids,
        }

    def _harvest_cb(state: dict) -> bool:
        state["worktree_harvest_checked"] = True
        return True

    builder = StateGraph()
    builder.add_node("prepare_context", _node_prepare_context)
    builder.add_node("execute_engine", _node_execute_engine)
    builder.add_node("harvest_and_distill", _node_harvest_and_distill)
    builder.set_entry_point("prepare_context")
    builder.add_edge("prepare_context", "execute_engine")
    builder.add_edge("execute_engine", "harvest_and_distill")
    builder.add_edge("harvest_and_distill", END)

    compiled_graph = builder.compile(
        checkpointer=active_ckpt,
        recursion_limit=10,
        stagnation_limit=4,
        worktree_harvest_check=True,
        harvest_callback=_harvest_cb,
    )

    monitor_stopped = False
    try:
        try:
            final_state = compiled_graph.invoke(
                {
                    "task_id": task_id,
                    "raw_goal": raw_goal,
                    "worktree": str(_workdir()),
                },
                config={"task_id": task_id},
            )
        except (_InterruptedShutdownError, NodeExecutionError) as exc:
            is_shutdown = (
                isinstance(exc, _InterruptedShutdownError)
                or isinstance(getattr(exc, "__cause__", None), _InterruptedShutdownError)
                or _SHUTDOWN_SIGNAL != 0
            )
            if not is_shutdown:
                raise
            _sync_attempt_ckpt_snapshot()
            monitor.stop(ok=False)
            monitor_stopped = True
            print(
                json.dumps(
                    {
                        "ok": False,
                        "interrupted": True,
                        "error": "interrupted_by_shutdown",
                    },
                    ensure_ascii=False,
                )
            )
            return EX_TEMPFAIL

        _sync_attempt_ckpt_snapshot()
        envelope = dict(final_state.get("envelope") or {"ok": False, "error": "both_engines_failed"})
        is_ok = bool(envelope.get("ok"))
        monitor.stop(ok=is_ok)
        monitor_stopped = True
        print(json.dumps(envelope, ensure_ascii=False))
        return 0
    finally:
        if not monitor_stopped:
            monitor.stop(ok=False)


if __name__ == "__main__":
    raise SystemExit(main())
