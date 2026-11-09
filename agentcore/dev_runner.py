#!/usr/bin/env python3
"""Development colleague runner for agentcore durable jobs.

Executes development colleague tasks in an isolated worktree with role contracts
and full coding tools (read, write, edit, bash). Concurrency is guarded by
cross-process worktree flock locks. Outputs structured report.json, report.md,
and manifest.json conforming to the agentcore worker protocol.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Callable, Dict, Optional


def resolve_platform_repo() -> Optional[Path]:
    """Dynamically locate host platform repository root without hardcoded business literals."""
    explicit = os.environ.get("PLATFORM_AUTO_TEST_ROOT", os.environ.get("AUTOTEST_ROOT", "")).strip()
    if explicit and Path(explicit).is_dir():
        return Path(explicit).resolve()
    candidates = [
        Path.cwd(),
        Path(__file__).resolve().parents[2],
        Path.home() / "code",
        Path.home(),
    ]
    for base in candidates:
        if not base.is_dir():
            continue
        if (base / "hermes_bridge").is_dir():
            return base.resolve()
        for sub in base.iterdir():
            if sub.is_dir() and (sub / "hermes_bridge").is_dir():
                return sub.resolve()
    return None


def _ensure_platform_importable() -> None:
    repo = resolve_platform_repo()
    if repo and str(repo) not in sys.path:
        sys.path.insert(0, str(repo))


def _atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _load_job(source=None) -> dict[str, Any]:
    if source is None:
        source = sys.stdin
    raw = source.read(131073)
    if len(raw) > 131072:
        raise ValueError("job_input_budget")
    job = json.loads(raw)
    if not isinstance(job, dict) or not isinstance(job.get("payload"), dict):
        raise ValueError("invalid_job")
    return job


def _goal(job: dict[str, Any]) -> str:
    goal = job["payload"].get("goal")
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 12000:
        raise ValueError("invalid_goal")
    return goal.strip()


def _policy(value: str) -> dict[str, Any]:
    policy = json.loads(Path(value).read_text(encoding="utf-8"))
    roots = policy.get("allowed_roots")
    if not isinstance(roots, list) or not roots:
        raise ValueError("invalid_policy")
    return policy


def _workdir(value: str, policy: dict[str, Any]) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise ValueError("workdir_missing")
    allowed = [Path(root).expanduser().resolve() for root in policy["allowed_roots"]]
    if not any(path == root or path.is_relative_to(root) for root in allowed):
        raise ValueError("workdir_outside_roots")
    return path


def _role(value: str) -> str:
    text = Path(value).read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("empty_role_contract")
    return text


def _detect_tool_stagnation(tool_calls: list[Any], threshold: int = 3) -> bool:
    """Detect if the execution loop is repeating identical tool calls without progress."""
    if not tool_calls or len(tool_calls) < threshold:
        return False
    recent = tool_calls[-threshold:]
    signatures = []
    for call in recent:
        if isinstance(call, dict):
            name = call.get("name") or call.get("function", {}).get("name") or ""
            args = call.get("args") or call.get("arguments") or call.get("function", {}).get("arguments") or ""
            try:
                args_str = json.dumps(args, sort_keys=True) if isinstance(args, (dict, list)) else str(args)
            except Exception:
                args_str = str(args)
            signatures.append((name, args_str))
        else:
            signatures.append(str(call))
    return len(signatures) == threshold and len(set(signatures)) == 1


@contextlib.contextmanager
def worktree_lock(workdir: Path, lock_root: Optional[Path] = None):
    """One writer per worktree, across processes."""
    if lock_root is None:
        lock_root = Path(os.environ.get("AGENTCORE_LOCK_DIR", "/tmp/agentcore-worktree-locks"))
    lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(str(workdir.resolve()).encode()).hexdigest()
    fd = os.open(lock_root / f"{key}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("worktree_busy") from None
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def run_dev_job(
    job: dict[str, Any],
    policy: dict[str, Any],
    role: str,
    workdir: Path,
    agent_dir: Path,
    model: str = "flash",
    provider: str = "ccp",
    tools: str = "read,write,edit,bash",
    timeout: float = 900.0,
    memory_file: str = "",
    sandbox: bool = False,
    output_dir: Optional[Path] = None,
    lock_root: Optional[Path] = None,
    runner_callable: Optional[Callable[[str, Any], dict[str, Any]]] = None,
    verification_executor: Optional[Callable[..., dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Execute a development task for a colleague role in an isolated worktree."""
    goal = _goal(job)
    from agentcore.verification_gate import (
        MAX_VERIFICATION_CHECKS, evaluate_verification_checks, normalize_verification_check,
    )
    # The queued task is data, not permission to run additional host commands.
    # Freeze administrator contracts before any model code or filesystem writes.
    configured = policy.get("verification_checks", [])
    fields = {"check_id", "command", "timeout_s", "scope", "safety_tier",
              "expected_exit_code", "critical", "env"}
    try:
        if (not isinstance(configured, list) or len(configured) > MAX_VERIFICATION_CHECKS
                or any(not isinstance(c, dict) or set(c) - fields for c in configured)):
            raise ValueError
        verification_checks = tuple(normalize_verification_check(c) for c in configured)
        if len({c.check_id for c in verification_checks}) != len(verification_checks):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ValueError("invalid_verification_policy") from None
    requested = job["payload"].get("verification_checks", [])
    try:
        if (not isinstance(requested, list) or len(requested) > MAX_VERIFICATION_CHECKS
                or any(not isinstance(c, dict) or set(c) - fields for c in requested)):
            raise ValueError
        if requested and tuple(normalize_verification_check(c) for c in requested) != verification_checks:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise PermissionError("verification_checks_not_authorized") from None
    require_verification = bool(policy.get("require_verification") or job["payload"].get("require_verification"))
    if require_verification and not verification_checks:
        raise ValueError("required_verification_checks_missing")
    if verification_executor is not None and not callable(verification_executor):
        raise ValueError("invalid_verification_executor")
    if sandbox and verification_checks and verification_executor is None:
        # The legacy model launcher does not contain this separate command path.
        # Never silently run a sandboxed task's verification on the host.
        raise RuntimeError("sandbox_verification_executor_required")

    # Single source of truth for model routing & financial overdraft guard (ADR-S1, ADR-S2):
    # Resolve via agentcore.model_routing instead of duplicating ad-hoc 'pro' -> 'flash' rules.
    from agentcore.model_routing import EXECUTION_ROLES, resolve_model_route
    raw_role = str(job["payload"].get("role") or "dev_runner").strip().lower()
    routing_role = raw_role if raw_role in EXECUTION_ROLES else "dev_runner"
    clean_model = (model or "").strip() or "flash"
    route = resolve_model_route("super-agent", role=routing_role, requested_model=clean_model)
    model = route["selected_model"] or "flash"
    degraded = bool(route.get("degraded"))
    degradation_reason = str(route.get("degradation_reason") or "")
    if degraded and degradation_reason:
        sys.stderr.write(degradation_reason + "\n")

    memory = ""
    if memory_file:
        try:
            memory = Path(memory_file).read_text(encoding="utf-8")[:12000]
        except OSError:
            memory = ""

    # Inject procedural skill guidance from SkillForge if available (EverOS Agent Track)
    skill_guidance = ""
    try:
        from agentcore.skill_forge import SkillForge
        forge = SkillForge()
        skill_guidance = forge.render_guidance(goal)
    except Exception:
        skill_guidance = ""

    prompt_parts = [role]
    if memory:
        prompt_parts.append(f"\n## 共享记忆快照\n{memory}")
    if skill_guidance:
        prompt_parts.append(f"\n{skill_guidance}")
    prompt_parts.append(f"\n## 任务\n{goal}\n")
    prompt = "\n".join(prompt_parts)

    agent_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    verification_receipt = None
    with worktree_lock(workdir, lock_root=lock_root):
        if runner_callable is None:
            raise RuntimeError("runner_adapter_required")
        raw_result = runner_callable(prompt, {
            "workdir": workdir,
            "agent_dir": agent_dir,
            "model": model,
            "provider": provider,
            "tools": tools,
            "timeout": timeout,
            "sandbox": sandbox,
        })
        payload = raw_result if isinstance(raw_result, dict) else raw_result.to_dict()

        ok = bool(payload.get("ok"))
        tool_calls = payload.get("tool_calls")
        if not tool_calls and isinstance(payload.get("result"), dict):
            tool_calls = payload["result"].get("tool_calls")
        if ok and _detect_tool_stagnation(tool_calls or []):
            ok = False
            payload["error"] = "tool_loop_stagnation_detected"
        if ok and verification_checks:
            # Keep the same worktree locked through verification; checking a
            # later task's files cannot certify this task's output.
            verification_receipt = evaluate_verification_checks(
                verification_checks, workdir, executor=verification_executor)
            if not verification_receipt.get("all_passed"):
                ok = False
        elif ok and require_verification and not verification_receipt:
            ok = False
            payload["error"] = "required_verification_missing_receipt"
        if ok and workdir.is_dir() and (workdir / ".git").exists():
            from agentcore.state_graph import check_worktree_harvest, WorktreeStrandedError
            try:
                check_worktree_harvest(workdir)
            except WorktreeStrandedError as exc:
                payload["worktree_harvest_warning"] = str(exc)
    out = (output_dir or Path.cwd()).resolve()
    try:
        out.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        pass
    artifact_paths = [str(out / name) for name in ("report.json", "report.md", "manifest.json")]

    execution_completed = bool(payload.get("ok")) and payload.get("error") != "tool_loop_stagnation_detected"
    verification_passed = bool(verification_receipt and verification_receipt.get("all_passed"))
    artifacts_valid = bool(out.is_dir())
    if not artifacts_valid and ok:
        ok = False
        if not payload.get("error_message") and not payload.get("error"):
            payload["error"] = "artifacts_destination_invalid"

    failure_decision = None
    err = payload.get("error_message") or payload.get("error")
    if not ok:
        if verification_receipt and not verification_receipt.get("all_passed"):
            err = f"verification_checks_failed: verdict={verification_receipt.get('verdict')}"
        elif not err:
            err = "execution_failed"
        from agentcore.failure_mender import decide_failure
        failure_decision = decide_failure(err, source=f"dev_runner:{job.get('job', 'unknown')}")

    report = {
        "ok": ok,
        "execution_completed": execution_completed,
        "verification_passed": verification_passed,
        "artifacts_valid": artifacts_valid,
        "role": job["payload"].get("role"),
        "job": job.get("job"),
        "attempt": job.get("attempt"),
        "workdir": str(workdir),
        "model": payload.get("model") or model,
        "degraded": degraded,
        "degradation_reason": degradation_reason,
        "provider": provider,
        "tool_calls": payload.get("tool_calls") or [],
        "final_text": payload.get("final_text") or "",
        "error": err,
        "events": payload.get("events") or [],
        "failure_decision": failure_decision,
        "verification_receipt": verification_receipt,
    }

    _atomic(out / "report.json", json.dumps(report, ensure_ascii=False, indent=2))
    _atomic(out / "report.md", f"# Development colleague report\n\n{payload.get('final_text') or err or ''}\n")
    _atomic(out / "manifest.json", json.dumps({
        "job": job.get("job"),
        "attempt": job.get("attempt"),
        "role": job["payload"].get("role"),
        "workdir": str(workdir),
        "model": payload.get("model") or model,
        "degraded": degraded,
        "degradation_reason": degradation_reason,
        "provider": provider,
        "tool_calls": payload.get("tool_calls") or [],
    }, ensure_ascii=False, indent=2))

    if ok:
        try:
            from agentcore.skill_forge import AgentCase, SkillForge
            forge = SkillForge()
            case = AgentCase(
                case_id=f"case_{job.get('job', 'dev')}_{job.get('attempt', 0)}",
                task_intent=goal[:200],
                approach=(payload.get("final_text") or "")[:500],
                quality_score=100.0 if not degraded else 85.0,
                role=str(job["payload"].get("role") or "cases"),
                verified_files=[str(p) for p in artifact_paths],
                approved=False,
            )
            forge.record_case(case)
        except Exception:
            pass

    if not ok:
        ret = {
            "ok": False,
            "execution_completed": execution_completed,
            "verification_passed": verification_passed,
            "artifacts_valid": artifacts_valid,
            "error": err or "execution_failed",
            "degraded": degraded,
            "degradation_reason": degradation_reason,
            "result": {"artifacts": artifact_paths},
        }
        if failure_decision:
            ret["failure_decision"] = failure_decision
        if verification_receipt:
            ret["verification_receipt"] = verification_receipt
        return ret

    ret = {
        "ok": True,
        "execution_completed": execution_completed,
        "verification_passed": verification_passed,
        "artifacts_valid": artifacts_valid,
        "degraded": degraded,
        "degradation_reason": degradation_reason,
        "result": {
            "role": job["payload"].get("role"),
            "job": job.get("job"),
            "attempt": job.get("attempt"),
            "summary": (payload.get("final_text") or "")[:2000],
            "model": payload.get("model") or model,
            "degraded": degraded,
            "degradation_reason": degradation_reason,
            "tool_calls": payload.get("tool_calls") or [],
            "artifacts": artifact_paths,
        },
    }
    if verification_receipt:
        ret["verification_receipt"] = verification_receipt
    return ret


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Development colleague runner for durable jobs",
                                     allow_abbrev=False)
    parser.add_argument("--policy", required=True, help="Path to policy JSON with allowed_roots")
    parser.add_argument("--role-contract", required=True, help="Path to role contract markdown")
    parser.add_argument("--workdir", required=True, help="Isolated working directory")
    parser.add_argument("--agent-dir", required=True, help="Agent configuration and cache dir")
    parser.add_argument("--model", default="flash")
    parser.add_argument("--provider", default="ccp")
    parser.add_argument("--tools", default="read,write,edit,bash")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--memory-file", default="")
    parser.add_argument("--sandbox", action="store_true")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--runner-adapter", default=None,
                        help="Administrator-owned module:callable(prompt, options)")
    parser.add_argument("--verification-adapter", default=None,
                        help="Administrator-owned module:callable(argv, timeout_s, workdir, *, env)")
    args = parser.parse_args(argv)

    try:
        job = _load_job(sys.stdin)
        policy = _policy(args.policy)
        workdir = _workdir(args.workdir, policy)
        out_p = Path(args.output_dir) if args.output_dir else Path.cwd()
        role = _role(args.role_contract)
        # Only trusted launch arguments select imports; queued payload is data.
        # Python diagnostics must not corrupt the single JSON stdout receipt.
        with contextlib.redirect_stdout(sys.stderr):
            import importlib
            import inspect

            adapters = {}
            for name, reference in (("runner_adapter", args.runner_adapter),
                                    ("verification_adapter", args.verification_adapter)):
                if reference is None:
                    continue
                try:
                    module, symbol = reference.split(":")
                    if not all(part.isascii() and part.isidentifier()
                               for part in (*module.split("."), symbol)):
                        raise ValueError
                    adapter = getattr(importlib.import_module(module), symbol)
                    if (not callable(adapter) or inspect.iscoroutinefunction(adapter)
                            or inspect.iscoroutinefunction(getattr(adapter, "__call__", None))):
                        raise ValueError
                    probe = (("", {}), {}) if name == "runner_adapter" else (
                        (("true",), 1.0, workdir), {"env": {}})
                    inspect.signature(adapter).bind(*probe[0], **probe[1])
                except Exception:
                    raise RuntimeError(f"{name}_unavailable") from None
                adapters[name] = adapter

            runner = None
            verifier = None
            if "runner_adapter" in adapters:
                def runner(prompt, options):
                    try:
                        value = adapters["runner_adapter"](prompt, options)
                        value = value if isinstance(value, dict) else value.to_dict()
                        if not isinstance(value, dict) or type(value.get("ok")) is not bool:
                            raise ValueError
                        return value
                    except Exception:
                        raise RuntimeError("runner_adapter_failed") from None
            if "verification_adapter" in adapters:
                def verifier(command, timeout_s, directory, *, env):
                    try:
                        value = adapters["verification_adapter"](
                            command, timeout_s, directory, env=env)
                        if (not isinstance(value, dict)
                                or (value.get("exit_code") is not None
                                    and type(value["exit_code"]) is not int)):
                            raise ValueError
                        return value
                    except Exception:
                        raise RuntimeError("verification_adapter_failed") from None

            result = run_dev_job(
                job,
                policy,
                role,
                workdir,
                Path(args.agent_dir).expanduser(),
                model=args.model,
                provider=args.provider,
                tools=args.tools,
                timeout=args.timeout,
                memory_file=args.memory_file,
                sandbox=args.sandbox,
                output_dir=out_p,
                runner_callable=runner,
                verification_executor=verifier,
            )
    except Exception as error:
        result = {"ok": False, "error": f"{type(error).__name__}: {error}"}

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
