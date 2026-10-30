"""`task_status` — answer "is it still running?" from the liveness ledger.

The tool is read-only. It never starts, polls in a loop, or kills a task. It
reconciles one host background process when asked (so an exited process gets its
real exit code instead of a guessed state), then reports the ledger's computed
verdict: state, elapsed, heartbeat age, phase, progress, bounded output tail.

The host supplies ``session_id`` as a dispatcher keyword; the model cannot forge
ownership in tool arguments. A task from another session is reported as
``unknown_task`` rather than leaking its label or output.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_CACHE: Dict[Any, Any] = {}
_CTX = None


def _load_liveness(engine_root: Optional[str] = None):
    """Import the canonical ledger, or load the checkout as a transient module."""
    try:
        from agentcore import liveness  # type: ignore
        return liveness
    except ModuleNotFoundError as error:
        if error.name not in ("agentcore", "agentcore.liveness"):
            raise
    if not isinstance(engine_root, str) or not Path(engine_root).is_absolute():
        raise ValueError("engine_root is required when agentcore is not importable")
    path = Path(engine_root).resolve(strict=True) / "agentcore" / "liveness.py"
    if not path.is_file():
        raise ValueError("engine_root does not contain agentcore/liveness.py")
    key = ("module", str(path))
    if key in _CACHE:
        return _CACHE[key]
    spec = importlib.util.spec_from_file_location("agentcore_liveness", path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load the liveness ledger")
    module = importlib.util.module_from_spec(spec)
    # The module has no relative imports, so a transient name is enough.
    sys.modules["agentcore_liveness"] = module
    spec.loader.exec_module(module)
    _CACHE[key] = module
    return module


def _ledger(ctx):
    module = _load_liveness(ctx.get_config("engine_root"))
    path = ctx.get_config("liveness_db") or ctx.get_config("db_path") or None
    try:
        stall = float(ctx.get_config("stall_seconds", module.DEFAULT_STALL_SECONDS))
    except (TypeError, ValueError):
        stall = module.DEFAULT_STALL_SECONDS
    key = ("ledger", str(path), stall)
    if key not in _CACHE:
        try:
            ledger = module.LivenessLedger(path, stall_seconds=stall)
            ledger.ensure_schema()
        except Exception:
            logger.warning("task_status ledger init failed path=%s", path, exc_info=True)
            raise
        _CACHE[key] = ledger
    return _CACHE[key]


def _session_id(session_id: Optional[str]) -> str:
    """Dispatcher keyword first, then the host's bound session environment.

    Plugin tool handlers do not always receive ``session_id`` even though the
    hook receives it; without the fallback a task registered by the hook is
    invisible to the tool (same profile, different owner string).
    """
    if isinstance(session_id, str) and session_id:
        return session_id
    try:
        from gateway.session_context import get_session_env  # type: ignore
        value = get_session_env("HERMES_SESSION_ID", "")
        if isinstance(value, str) and value:
            return value
    except Exception:
        pass
    return ""


def _owner(ctx, session_id: Optional[str]) -> str:
    profile = getattr(ctx, "profile_name", "") or "local"
    return "%s:%s" % (profile, _session_id(session_id) or "local")


def _process_registry():
    try:
        from tools.process_registry import process_registry  # type: ignore
        return process_registry
    except Exception:  # host surface absent in an isolated test runtime
        return None


def _reconcile(ledger, task: Dict[str, Any]) -> Dict[str, Any]:
    """Observe a host background process once; persist its real exit, never a guess."""
    if task.get("kind") != "terminal" or not task.get("pid"):
        return task
    registry = _process_registry()
    if registry is None:
        return task
    try:
        result = registry.poll(task["task_id"])
    except Exception:  # noqa: BLE001 — a status read must not break the tool
        logger.debug("process poll failed for %s", task.get("task_id"), exc_info=True)
        return task
    if not isinstance(result, dict):
        return task
    status = result.get("status")
    if status == "running":
        return task
    code = result.get("exit_code")
    if status in ("exited", "completed", "failed") or code is not None:
        tail = result.get("output_preview") or result.get("output") or ""
        if code is None and status in ("completed", "success"):
            ledger.finish(task["task_id"], "done", tail=tail,
                          detail="host process %s" % status)
        elif code is not None:
            ledger.record_exit(task["task_id"], code, tail=tail,
                               detail="host process %s" % (status or "exited"))
        else:
            return task  # no exit status and no success marker: do not guess
        return ledger.get(task["task_id"]) or task
    # ``not_found`` proves neither success nor failure; sweep decides from pid evidence.
    return task


def _handle(args, *, session_id=None, task_id=None, user_task=None):
    if not isinstance(args, dict):
        return json.dumps({"ok": False, "error": "arguments must be an object"})
    action = args.get("action")
    if action not in ("list", "inspect", "tail"):
        return json.dumps({"ok": False, "error": "action must be list, inspect or tail"})
    try:
        ledger = _ledger(_CTX)
        ledger.ensure_schema()  # self-heal a concurrent first writer / stale empty file
        owner = _owner(_CTX, session_id)
        if action == "list":
            try:
                limit = int(args.get("limit", 50))
            except (TypeError, ValueError):
                limit = 50
            # Classify the non-terminal rows before reporting them. Without this, a row whose
            # process died keeps claiming ``running`` until someone happens to inspect *that*
            # task; the caller asking "what am I running" is exactly the moment to fix it.
            # ``sweep`` writes only rows whose classification changed and never guesses from
            # silence: a live pid stays ``running``, a gone pid becomes ``lost``, and no pid
            # plus a stale heartbeat becomes ``stalled``.
            try:
                ledger.sweep(owner=owner)
            except Exception:  # noqa: BLE001 — a failed sweep must not break the read
                logger.debug("task_status sweep failed", exc_info=True)
            tasks = ledger.list_tasks(owner=owner,
                                      active_only=bool(args.get("active_only", True)),
                                      limit=limit)
            # The host registry can still know the real exit; that beats a bare ``lost``.
            tasks = [_reconcile(ledger, row) for row in tasks]
            tasks = [ledger.inspect(row["task_id"], require_owner=owner) for row in tasks]
            return json.dumps({"ok": True, "owner": owner, "tasks": tasks},
                              ensure_ascii=False)
        target = args.get("task_id")
        if not isinstance(target, str) or not target:
            return json.dumps({"ok": False, "error": "task_id is required"})
        row = ledger.get(target, require_owner=owner)
        if row is None:
            return json.dumps({"ok": False, "error": "unknown_task", "task_id": target},
                              ensure_ascii=False)
        row = _reconcile(ledger, row)
        if action == "inspect":
            return json.dumps(ledger.inspect(target, require_owner=owner), ensure_ascii=False)
        # tail: prefer the host's full rolling log, fall back to the ledger's bounded tail.
        registry = _process_registry()
        if row.get("kind") == "terminal" and registry is not None:
            try:
                log = registry.read_log(target, limit=int(args.get("limit", 100)))
                if isinstance(log, dict) and log.get("output") is not None:
                    return json.dumps({"ok": True, "task_id": target,
                                       "output": log.get("output"),
                                       "total_lines": log.get("total_lines")},
                                      ensure_ascii=False)
            except Exception:  # noqa: BLE001
                logger.debug("process log read failed for %s", target, exc_info=True)
        return json.dumps({"ok": True, "task_id": target, "output": row.get("tail") or ""},
                          ensure_ascii=False)
    except Exception as error:  # noqa: BLE001 — report the failure class, never a stack
        logger.warning("task_status failed", exc_info=True)
        return json.dumps({"ok": False, "error": "liveness backend unavailable",
                           "error_type": type(error).__name__})


_SCHEMA = {
    "name": "task_status",
    "description": (
        "Read the liveness state of a long-running task: state, elapsed seconds, "
        "heartbeat age, phase, progress, pid health and a bounded output tail. "
        "When liveness_note is present, tell the user its meaning; a visible process "
        "with a stale heartbeat does not prove continued progress. "
        "Use once when the user asks whether work is still running; never loop or sleep. "
        "Returns unknown_task when no record belongs to this session."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ["list", "inspect", "tail"]},
            "task_id": {"type": "string", "description": "Task/process id from the start receipt."},
            "active_only": {"type": "boolean", "description": "For list: exclude terminal tasks."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
        },
        "required": ["action"],
    },
}


def register(ctx) -> None:
    global _CTX
    if ctx.get_config("enabled", False) is not True:
        return
    _CTX = ctx
    ctx.register_tool(name="task_status", toolset="task_status", description=_SCHEMA["description"],
                      schema=_SCHEMA, handler=_handle)
    ctx.register_system_prompt_section(
        "task-liveness",
        "When the user asks whether a long task is still running, call task_status once "
        "(inspect with its task id, or list active tasks). Report state, elapsed, heartbeat "
        "age, phase and the bounded tail as evidence. Do not loop, sleep or re-run the task; "
        "if the ledger has no record, say unknown and name what was checked.",
        position="after_memory", max_chars=800)
