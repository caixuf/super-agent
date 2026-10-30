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
) -> dict[str, Any]:
    """Execute a development task for a colleague role in an isolated worktree."""
    goal = _goal(job)
    memory = ""
    if memory_file:
        try:
            memory = Path(memory_file).read_text(encoding="utf-8")[:12000]
        except OSError:
            memory = ""
    prompt = f"{role}\n\n## 共享记忆快照\n{memory}\n\n## 任务\n{goal}\n"

    agent_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    with worktree_lock(workdir, lock_root=lock_root):
        if runner_callable is not None:
            raw_result = runner_callable(prompt, {
                "workdir": workdir,
                "agent_dir": agent_dir,
                "model": model,
                "provider": provider,
                "tools": tools,
                "timeout": timeout,
            })
            payload = raw_result if isinstance(raw_result, dict) else raw_result.to_dict()
        else:
            _ensure_platform_importable()
            import importlib
            try:
                pi_mod = importlib.import_module("hermes_bridge.pi_executor")
                ccp_mod = importlib.import_module("hermes_bridge.pi_executor.ccp_config")
                build_argv = getattr(pi_mod, "build_argv")
                copy_user_catalog = getattr(pi_mod, "copy_user_catalog")
                resolve_ccp_credentials = getattr(pi_mod, "resolve_ccp_credentials")
                resolve_cli_path = getattr(pi_mod, "resolve_cli_path")
                resolve_node = getattr(pi_mod, "resolve_node")
                run_pi = getattr(pi_mod, "run_pi")
                write_models_json = getattr(pi_mod, "write_models_json")
                CCP_KEY_ENV_VAR = getattr(ccp_mod, "CCP_KEY_ENV_VAR")
            except (ImportError, AttributeError) as err:
                raise RuntimeError(f"pi_executor_unavailable: {err}") from err

            env_extra: dict[str, str] = {}
            if provider == "ccp":
                credentials = resolve_ccp_credentials(alias=model)
                write_models_json(agent_dir, base_url=credentials.base_url, alias=credentials.alias)
                env_extra[CCP_KEY_ENV_VAR] = credentials.api_key
            else:
                copy_user_catalog(agent_dir)

            kwargs: dict[str, Any] = dict(
                workdir=workdir,
                agent_dir=agent_dir,
                model=model,
                provider=provider,
                tools=tools,
                timeout_s=timeout,
                env_extra=env_extra,
            )
            if sandbox:
                cli = resolve_cli_path(os.environ.get("PI_EXECUTOR_CLI"))
                node = resolve_node(os.environ.get("PI_NODE_BIN"))
                inner = [node, *build_argv(prompt, cli_path=cli, model=model,
                                           provider=provider, tools=tools)]
                kwargs["command_override"] = [
                    "/usr/bin/bwrap", "--die-with-parent",
                    "--ro-bind", "/", "/",
                    "--tmpfs", "/tmp", "--dev", "/dev", "--proc", "/proc",
                    "--bind", str(workdir), str(workdir),
                    "--bind", str(agent_dir), str(agent_dir),
                    "--chdir", str(workdir),
                    *inner,
                ]
            pi_res = run_pi(prompt, **kwargs)
            payload = pi_res.to_dict()

    ok = bool(payload.get("ok"))
    out = (output_dir or Path.cwd()).resolve()

    # Optional verification checks gate
    verification_checks = job["payload"].get("verification_checks")
    verification_receipt = None
    if ok and verification_checks:
        from agentcore.verification_gate import evaluate_verification_checks
        verification_receipt = evaluate_verification_checks(verification_checks, workdir)
        if not verification_receipt.get("all_passed"):
            ok = False

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
        "role": job["payload"].get("role"),
        "job": job.get("job"),
        "attempt": job.get("attempt"),
        "workdir": str(workdir),
        "model": payload.get("model") or model,
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
        "provider": provider,
        "tool_calls": payload.get("tool_calls") or [],
    }, ensure_ascii=False, indent=2))

    artifact_paths = [str(out / name) for name in ("report.json", "report.md", "manifest.json")]
    if not ok:
        ret = {
            "ok": False,
            "error": err or "execution_failed",
            "result": {"artifacts": artifact_paths},
        }
        if failure_decision:
            ret["failure_decision"] = failure_decision
        if verification_receipt:
            ret["verification_receipt"] = verification_receipt
        return ret

    ret = {
        "ok": True,
        "result": {
            "role": job["payload"].get("role"),
            "job": job.get("job"),
            "attempt": job.get("attempt"),
            "summary": (payload.get("final_text") or "")[:2000],
            "model": payload.get("model") or model,
            "tool_calls": payload.get("tool_calls") or [],
            "artifacts": artifact_paths,
        },
    }
    if verification_receipt:
        ret["verification_receipt"] = verification_receipt
    return ret


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Development colleague runner for durable jobs")
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
    args, _unknown = parser.parse_known_args(argv)

    try:
        job = _load_job(sys.stdin)
        policy = _policy(args.policy)
        workdir = _workdir(args.workdir, policy)
        out_p = Path(args.output_dir) if args.output_dir else Path.cwd()
        result = run_dev_job(
            job,
            policy,
            _role(args.role_contract),
            workdir,
            Path(args.agent_dir).expanduser(),
            model=args.model,
            provider=args.provider,
            tools=args.tools,
            timeout=args.timeout,
            memory_file=args.memory_file,
            sandbox=args.sandbox,
            output_dir=out_p,
        )
    except Exception as error:
        result = {"ok": False, "error": f"{type(error).__name__}: {error}"}

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
