"""task-progress — one editable entry per long task, driven by the liveness ledger.

Why
---
A long task today is either silent (the user asks "还在做吗") or noisy (one message per
event). This plugin gives every tracked long task **one message** that is *edited in place*
as the task actually changes, and nothing else:

* the entry opens once, when a background process is spawned; all background jobs spawned
  in the same turn share **one** entry (a turn is what the user asked for);
* updates come from ``agentcore.liveness`` evidence (state / phase / progress / heartbeat)
  plus the host registry's *real* exit — no percentage, finish time or next step is invented;
* interim tool output never opens a message: updates are edits, deduplicated by
  :mod:`agentcore.task_progress` and ordered/terminal-locked by :mod:`agentcore.card_store`;
* a terminal state is write-once, so a late progress frame cannot overwrite a result.

Non-model queries
-----------------
``/longtasks`` and ``/longtask <id>`` are registered as slash commands. The host dispatches
them **before** the turn and inside the session's env scope, so asking for progress costs no
model call and does not depend on the model remembering to look.

Transport
---------
The host's public ``PluginContext.post_message`` handle (``message.edit``) is the only
sender; one handle per process, one message per entry. A restart loses the in-memory
handle, so the plugin re-resolves the recorded route once through the host's plugin-message
bridge and edits the *same* message; if that is unavailable it records ``entry_frozen`` and
never posts a duplicate.
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import hashlib
import json
import logging
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_CTX = None
_ENGINE = ("liveness", "task_card", "card_store", "task_progress")
_DEFAULT_POLL_SECONDS = 10.0
_DEFAULT_MIN_INTERVAL = 60.0
_DEFAULT_MAX_TRACKED = 20
_DEFAULT_RECONCILE_LIMIT = 20


# --------------------------------------------------------------------------- #
# Engine loading (installed package first, administrator checkout second)
# --------------------------------------------------------------------------- #

def _engine_modules(engine_root: Optional[str] = None) -> dict:
    try:
        return {name: importlib.import_module("agentcore." + name) for name in _ENGINE}
    except ModuleNotFoundError as error:
        if error.name not in {"agentcore"} | {"agentcore." + name for name in _ENGINE}:
            raise
    if not isinstance(engine_root, str) or not Path(engine_root).is_absolute():
        raise ValueError("engine_root is required when the engine package is unavailable")
    directory = Path(engine_root).resolve(strict=True) / "agentcore"
    missing = [name + ".py" for name in _ENGINE if not (directory / (name + ".py")).is_file()]
    if missing:
        raise ValueError("engine_root is missing canonical modules: " + ", ".join(missing))
    saved = {name: module for name, module in sys.modules.items()
             if name == "agentcore" or name.startswith("agentcore.")}
    for name in saved:
        del sys.modules[name]
    package = types.ModuleType("agentcore")
    package.__path__ = [str(directory)]
    package.__package__ = "agentcore"
    sys.modules["agentcore"] = package
    loaded = {}
    try:
        for name in _ENGINE:
            spec = importlib.util.spec_from_file_location("agentcore." + name,
                                                          directory / (name + ".py"))
            if spec is None or spec.loader is None:
                raise ImportError("canonical engine unavailable: " + name)
            module = importlib.util.module_from_spec(spec)
            sys.modules["agentcore." + name] = module
            spec.loader.exec_module(module)
            loaded[name] = module
        return dict(loaded)
    finally:
        for name in [name for name in list(sys.modules)
                     if name == "agentcore" or name.startswith("agentcore.")]:
            del sys.modules[name]
        sys.modules.update(saved)


def _parse_result(result: Any) -> Optional[dict]:
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            value = json.loads(result)
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None
    return None


def _background_id(payload: dict) -> Optional[str]:
    """The tracked background process a ``terminal`` result names, if it names one."""
    for key in ("session_id", "yielded_session_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.startswith("proc_") and len(value) > 5:
            return value
    return None


def _registry():
    """The host's process registry, when this runtime has one (evidence, never a guess)."""
    try:
        from tools.process_registry import process_registry  # type: ignore
        return process_registry
    except Exception:
        return None


def _reconcile(ledger, task_id: str) -> None:
    """Persist the *real* exit the host registry observed; silence proves nothing.

    Without ``notify_on_complete`` nothing else closes the ledger row, so a finished job
    would otherwise stay ``running`` until a sweep could only call it ``lost``.
    """
    registry = _registry()
    if registry is None:
        return
    try:
        result = registry.poll(task_id)
    except Exception:
        logger.debug("task-progress: poll failed for %s", task_id, exc_info=True)
        return
    if not isinstance(result, dict):
        return
    status = result.get("status")
    code = result.get("exit_code")
    if status == "running":
        return
    tail = result.get("output_preview") or result.get("output") or ""
    try:
        if code is not None and status not in (None, "not_found", "unknown"):
            ledger.record_exit(task_id, int(code), tail=tail, detail="host process %s" % status)
        elif status in ("completed", "success"):
            ledger.finish(task_id, "done", tail=tail, detail="host process %s" % status)
    except Exception:
        logger.debug("task-progress: reconcile write failed for %s", task_id, exc_info=True)


# --------------------------------------------------------------------------- #
# Per-entry bookkeeping
# --------------------------------------------------------------------------- #

class _Entry:
    """One editable entry shared by a foreground turn and its background tasks."""

    __slots__ = ("key", "message", "writer", "tasks", "foreground", "session_key",
                 "snap", "last_emit_at", "final")

    def __init__(self, key: str, message: Any, tasks, *, foreground=None, session_key=""):
        self.key = key
        self.message = message
        self.writer = None
        self.tasks = list(tasks)
        self.foreground = dict(foreground) if isinstance(foreground, dict) else None
        self.session_key = session_key
        self.snap = None
        self.last_emit_at = 0.0
        self.final = False


def _settings(ctx) -> dict:
    return {
        "enabled": ctx.get_config("enabled", False) is True,
        "engine_root": ctx.get_config("engine_root"),
        "liveness_db": ctx.get_config("liveness_db"),
        "poll_seconds": ctx.get_config("poll_seconds", _DEFAULT_POLL_SECONDS),
        "min_interval": ctx.get_config("min_interval", _DEFAULT_MIN_INTERVAL),
        "max_tracked": ctx.get_config("max_tracked", _DEFAULT_MAX_TRACKED),
        "store_path": ctx.get_config("store_path"),
        "resume_after_restart": ctx.get_config("resume_after_restart", True) is not False,
        "foreground_min_seconds": ctx.get_config("foreground_min_seconds", 12),
    }


def _store(ctx, modules: dict, settings: dict):
    path = settings.get("store_path")
    if not isinstance(path, str) or not path:
        state = getattr(ctx, "state", None)
        base = getattr(state, "data_dir", None) if state is not None else None
        if base is None:
            raise ValueError("store_path is required when the host offers no plugin data dir")
        path = str(Path(base) / "cards.db")
    return modules["card_store"].CardStore(path)


def _resolve_target(ctx, session_key: str):
    from gateway import plugin_messages
    from gateway.run import _gateway_runner_ref
    runner = _gateway_runner_ref()
    if runner is None:
        return None, "no_gateway"
    target, refusal = plugin_messages.resolve(
        runner, session_key=session_key, route=None,
        plugin_home=ctx._manager.home_path, plugin_id=ctx.plugin_id)
    if target is None:
        return None, (refusal or {}).get("error", "no_conversation")
    return target, None


def _run_on_loop(coro, timeout: float = 15.0):
    from gateway.run import _gateway_runner_ref
    runner = _gateway_runner_ref()
    loop = getattr(runner, "_gateway_loop", None) if runner is not None else None
    if loop is None or loop.is_closed():
        raise RuntimeError("gateway loop unavailable")
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)


def _resume_edit(ctx, session_key: str, message_id: str, text: str) -> dict:
    """Edit the recorded message after a restart; never posts a second entry."""
    from gateway import plugin_messages
    target, error = _resolve_target(ctx, session_key)
    if target is None:
        return {"ok": False, "error": error or "no_conversation"}
    return _run_on_loop(plugin_messages.edit(target, message_id, text, plugin_id=ctx.plugin_id))


def _capture_session_key() -> str:
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_SESSION_KEY", "") or ""
    except Exception:
        return ""


def _post_to_session(ctx, session_key: str, text: str) -> dict:
    if not session_key:
        # Synchronous hooks already run in the host's bound conversation scope.
        try:
            handle = ctx.post_message(text)
            return {"ok": bool(getattr(handle, "ok", False)),
                    "message_id": getattr(handle, "message_id", None), "handle": handle}
        except Exception:
            return {"ok": False, "error": "no_conversation"}
    from gateway import plugin_messages
    target, error = _resolve_target(ctx, session_key)
    if target is None:
        return {"ok": False, "error": error or "no_conversation"}
    return _run_on_loop(plugin_messages.post(target, text, plugin_id=ctx.plugin_id))


def _edit_to_session(ctx, session_key: str, message_id: str, text: str) -> dict:
    if not session_key:
        return {"ok": False, "error": "no_conversation"}
    from gateway import plugin_messages
    target, error = _resolve_target(ctx, session_key)
    if target is None:
        return {"ok": False, "error": error or "no_conversation"}
    return _run_on_loop(plugin_messages.edit(target, message_id, text, plugin_id=ctx.plugin_id))


class _RouteMessage:
    def __init__(self, ctx, session_key: str, message_id: str):
        self.ctx, self.session_key, self.message_id = ctx, session_key, message_id

    def edit(self, text: str):
        return _edit_to_session(self.ctx, self.session_key, self.message_id, text)


def _entry_key(turn_id: str, session_id: str, task_id: str) -> str:
    """One entry per session turn; without a turn id, one entry per task."""
    if isinstance(turn_id, str) and turn_id.strip():
        if isinstance(session_id, str) and session_id.strip():
            return "turn:%s:%s" % (session_id.strip()[:48], turn_id.strip()[:64])
        return "turn:%s" % turn_id.strip()[:64]
    if isinstance(session_id, str) and session_id.strip():
        return "turn:%s:%s" % (session_id.strip()[:48], task_id)
    return "task:%s" % task_id


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #

def register(ctx) -> None:
    global _CTX
    settings = _settings(ctx)
    if not settings["enabled"]:
        return
    modules = _engine_modules(settings["engine_root"])
    store = _store(ctx, modules, settings)
    ledger = modules["liveness"].LivenessLedger(settings["liveness_db"])
    progress = modules["task_progress"]
    card_state = modules["task_card"].CardState
    state = getattr(ctx, "state", None)
    entries: Dict[str, _Entry] = {}
    lock = threading.Lock()
    stop = threading.Event()

    def _state_get(key, default):
        if state is None:
            return default
        try:
            return state.get(key, default)
        except Exception:
            return default

    def _state_set(key, value):
        if state is None:
            return
        try:
            state.set(key, value)
        except Exception:
            logger.debug("task-progress: state write failed (%s)", key, exc_info=True)

    def _record_result(entry: _Entry, result: dict) -> None:
        ok = bool((result or {}).get("ok"))
        _state_set("last_delivery", {"entry": entry.key, "ok": ok,
                                     "error": (result or {}).get("error"),
                                     "edited": bool((result or {}).get("edited")),
                                     "at": time.time()})

    def _publish(entry: _Entry, text: str, *, final: bool) -> dict:
        if entry.final:
            return {"ok": False, "error": "entry_terminal"}
        try:
            result = entry.message.edit(text) if entry.message is not None else None
        except Exception as exc:
            result = {"ok": False, "error": type(exc).__name__}
        if result is None:
            result = {"ok": False, "error": "no_live_handle"}
        if final:
            entry.final = True
        _record_result(entry, result)
        return result

    def _snaps(entry: _Entry) -> list:
        snaps = []
        for task_id in entry.tasks:
            _reconcile(ledger, task_id)
            row = ledger.get(task_id)
            snaps.append(progress.snapshot(row, now=time.time()) if row else
                         {"task_id": task_id, "verdict": "unknown", "state": "unknown",
                          "label": "", "phase": "", "percent": None, "elapsed": 0,
                          "detail": "", "artifacts": (), "terminal": False})
        return snaps

    def _title(entry: _Entry, snaps: list) -> str:
        for snap in snaps:
            if snap.get("label"):
                return snap["label"][:80]
        return "任务"

    def _open(payload: dict, *, turn_id: str, session_id: str) -> None:
        task_id = _background_id(payload)
        if not task_id:
            return
        key = _entry_key(turn_id, session_id, task_id)
        with lock:
            existing = entries.get(key)
            if existing is not None:
                if task_id not in existing.tasks:
                    existing.tasks.append(task_id)
                    existing.snap = None  # force an update for the new member
                return
            if len(entries) >= int(settings["max_tracked"]):
                logger.debug("task-progress: tracking bound reached, skipping %s", task_id)
                return
        snaps = [_first_snap(ledger, progress, task_id)]
        # A turn's later members are discovered by the poller; opening now keeps the entry
        # born with the task instead of after its first phase change.
        text = progress.render_many(snaps, title=_snaps_label(snaps))
        message = ctx.post_message(text)
        if message is None or not getattr(message, "ok", False):
            logger.info("task-progress: no conversation for %s (%s)", task_id,
                        getattr(message, "error", None))
            return
        entry = _Entry(key, message, [task_id])
        try:
            store.bind(key, "", str(getattr(message, "message_id", "") or ""))
            entry.writer = store.writer(key)
        except Exception:
            logger.warning("task-progress: could not bind %s", key, exc_info=True)
            return
        entry.snap = progress.aggregate(snaps)
        entry.last_emit_at = time.time()
        if entry.snap.get("terminal"):
            # The process finished before the entry opened: the first text is the last one.
            entry.final = True
            _close_card(store, entry, card_state, progress)
        with lock:
            entries[key] = entry
        session_key = ""
        try:
            from gateway.session_context import get_session_env
            session_key = get_session_env("HERMES_SESSION_KEY", "") or ""
        except Exception:
            session_key = ""
        members = dict(_state_get("entries", {}) or {})
        members[key] = {"session_key": session_key, "tasks": list(entry.tasks),
                        "opened_at": time.time()}
        _state_set("entries", members)

    def _close_card(store, entry: _Entry, card_state, progress) -> None:
        snap = entry.snap or {}
        try:
            handle = store.load(entry.key)
            if handle is not None and not handle.state.terminal:
                handle.state = progress.card_state({"state": snap.get("verdict")})
                store.save(handle)
        except Exception:
            logger.debug("task-progress: closing %s failed", entry.key, exc_info=True)

    def _poll_once() -> None:
        now = time.time()
        with lock:
            current = list(entries.items())
        for key, entry in current:
            if entry.final:
                continue
            snaps = _snaps(entry)
            head = progress.aggregate(snaps)
            if not progress.should_emit(entry.snap, head, last_emit_at=entry.last_emit_at,
                                        now=now, min_interval=float(settings["min_interval"])):
                continue
            text = progress.render_many(snaps, title=_title(entry, snaps))
            final = bool(head.get("terminal"))
            result = _publish(entry, text, final=final)
            entry.snap = head
            entry.last_emit_at = now
            if final:
                if not result.get("ok"):
                    # Delivery is unproven: keep it open so the next pass retries the edit.
                    entry.final = False
                    continue
                _close_card(store, entry, card_state, progress)

    def _resume() -> None:
        """After a restart: edit the recorded entry once instead of opening a new one."""
        if not settings["resume_after_restart"]:
            return
        members = dict(_state_get("entries", {}) or {})
        for handle in store.active()[:_DEFAULT_RECONCILE_LIMIT]:
            record = members.get(handle.task_id) or {}
            tasks = [t for t in (record.get("tasks") or []) if isinstance(t, str)]
            session_key = record.get("session_key") or ""
            snaps = [_first_snap(ledger, progress, task) for task in tasks]
            head = progress.aggregate(snaps)
            if not session_key or not handle.message_id:
                if head.get("terminal"):
                    handle.state = progress.card_state({"state": head.get("verdict")})
                    store.save(handle)
                continue
            text = progress.render_many(snaps, title=_snaps_label(snaps))
            try:
                result = _resume_edit(ctx, session_key, handle.message_id, text)
            except Exception as error:
                result = {"ok": False, "error": type(error).__name__}
            if not result.get("ok"):
                _state_set("entry_frozen", {"entry": handle.task_id,
                                            "error": result.get("error"), "at": time.time()})
                continue
            if head.get("terminal"):
                handle.state = progress.card_state({"state": head.get("verdict")})
                store.save(handle)

    def _loop() -> None:
        try:
            _resume()
        except Exception:
            logger.warning("task-progress: resume pass failed", exc_info=True)
        interval = max(2.0, float(settings["poll_seconds"]))
        while not stop.wait(interval):
            try:
                _poll_once()
            except Exception:
                logger.warning("task-progress: poll pass failed", exc_info=True)

    def _on_post_tool_call(tool_name: str = "", args: Any = None, result: Any = None,
                           session_id: str = "", turn_id: str = "", **_: Any) -> None:
        if tool_name != "terminal":
            return None
        payload = _parse_result(result)
        if payload is None:
            return None
        try:
            _open(payload, turn_id=turn_id or "", session_id=session_id or "")
        except Exception:
            logger.warning("task-progress: open failed", exc_info=True)
        return None

    # ── 不进模型的查询入口 ────────────────────────────────────────────────
    def _session_id() -> str:
        try:
            from gateway.session_context import get_session_env
            return get_session_env("HERMES_SESSION_ID", "") or ""
        except Exception:
            return ""

    def _cmd_longtasks(raw_args: str = "") -> str:
        owner = "%s:%s" % (getattr(ctx, "profile_name", "") or "local", _session_id() or "local")
        rows = ledger.list_tasks(owner=owner, active_only=False, limit=20)
        if not rows:
            return "本会话没有登记过的长任务。"
        lines = []
        for row in rows:
            snap = progress.snapshot(row, now=time.time())
            word = {"queued": "已接收", "running": "进行中"}.get(snap["verdict"], snap["verdict"])
            minutes, seconds = divmod(int(snap["elapsed"]), 60)
            elapsed = "%d分%02d秒" % (minutes, seconds) if minutes else "%d秒" % seconds
            lines.append("· `%s` %s · 已用 %s%s" % (snap["task_id"][:20], word, elapsed,
                                                    (" · " + snap["phase"]) if snap.get("phase") else ""))
        return "本会话的长任务（%d）：\n%s" % (len(lines), "\n".join(lines))

    def _cmd_longtask(raw_args: str = "") -> str:
        text = (raw_args or "").strip()
        task = text.split()[0] if text else ""
        if not task:
            return "用法：/longtask <任务号>（任务号见 /longtasks）"
        owner = "%s:%s" % (getattr(ctx, "profile_name", "") or "local", _session_id() or "local")
        row = ledger.get(task, require_owner=owner)
        if row is None:
            return "unknown_task：本会话没有这个任务（不会替你猜别的会话的任务）。"
        return progress.render(progress.snapshot(row, now=time.time()),
                               title=str(row.get("label") or task))

    register_command = getattr(ctx, "register_command", None)
    if callable(register_command):
        register_command("longtasks", _cmd_longtasks, "本会话的长任务进度（不启动模型回合）")
        register_command("longtask", _cmd_longtask, "一个长任务的当前状态：/longtask <任务号>",
                         args_hint="<任务号>")

    ctx.register_hook("post_tool_call", _on_post_tool_call)
    thread = threading.Thread(target=_loop, name="task-progress-poller", daemon=True)
    thread.start()

    def _stop():
        stop.set()
        thread.join(timeout=5.0)

    on_unload = getattr(ctx, "on_unload", None)
    if callable(on_unload):
        try:
            on_unload(_stop)
        except TypeError:
            pass
    _CTX = ctx
    logger.debug("task-progress registered: db=%s poll=%ss", settings["liveness_db"],
                 settings["poll_seconds"])


def _first_snap(ledger, progress, task_id: str) -> dict:
    row = ledger.get(task_id)
    if row is None:
        return {"task_id": task_id, "verdict": "unknown", "state": "unknown", "label": "",
                "phase": "", "percent": None, "elapsed": 0, "detail": "", "artifacts": (),
                "terminal": False}
    return progress.snapshot(row, now=time.time())


def _snaps_label(snaps) -> str:
    for snap in snaps or ():
        if snap.get("label"):
            return snap["label"][:80]
    return "任务"
