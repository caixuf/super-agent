"""task-progress — one editable entry per long task, driven by the liveness ledger.

Why
---
A long task today is either silent (the user asks "还在做吗") or noisy (one message per
event). This plugin gives every tracked long task **one message** that is *edited in place*
as the task actually changes, and nothing else:

* the entry is opened once, at the moment a background process is spawned;
* updates come from ``agentcore.liveness`` evidence only (state / phase / progress /
  heartbeat) — no percentages or finish times are invented;
* interim tool output never opens a message: updates are edits, deduplicated by
  :mod:`agentcore.task_progress` and ordered/terminal-locked by :mod:`agentcore.card_store`
  + :mod:`agentcore.card_emit`;
* a terminal state is write-once, so a late progress frame cannot overwrite a result.

Transport
---------
The host's public ``PluginContext.post_message`` handle (``message.edit``) is the only
sender; one handle per process, one message per task. A restart loses the in-memory
handle, so the plugin re-resolves the recorded route once through the host's plugin-message
bridge and edits the *same* message instead of posting a second one; if that is not
available it records ``entry_frozen`` and never posts a duplicate.
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import logging
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_CTX = None
_CACHE: dict = {}
_ENGINE = ("liveness", "task_card", "card_store", "card_emit", "task_progress")
_DEFAULT_POLL_SECONDS = 10.0
_DEFAULT_MIN_INTERVAL = 60.0
_DEFAULT_MAX_TRACKED = 20
_DEFAULT_RECONCILE_LIMIT = 20


def _engine_modules(engine_root: Optional[str] = None) -> dict:
    """``{name: module}`` for the canonical engine modules, without touching ``sys.path``."""
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


class _Entry:
    """In-memory bookkeeping for one open entry (the durable identity is in CardStore)."""

    __slots__ = ("task_id", "message", "emitter", "snap", "last_emit_at", "final", "errors")

    def __init__(self, task_id: str, message: Any):
        self.task_id = task_id
        self.message = message
        self.emitter = None
        self.snap = None
        self.last_emit_at = 0.0
        self.final = False
        self.errors = 0


def _text_for(view: Any) -> str:
    """Render a ``CardView`` into the one editable entry's text."""
    lines = ["**%s**" % (view.title or "任务")[:90]]
    word = {"queued": "已接收", "running": "进行中", "done": "已完成",
            "failed": "未完成", "cancelled": "已取消"}.get(view.state, view.state or "进行中")
    lines.append("状态：%s" % word)
    if view.phase:
        lines.append("当前：%s" % view.phase)
    if view.note:
        lines.append("已用：%s" % view.note)
    if view.artifacts:
        lines.append("产物：%s" % "、".join(view.artifacts))
    return "\n".join(lines)


class _Transport:
    """Publishes card views onto the entry's single message."""

    def __init__(self, entry: _Entry, *, on_result):
        self._entry = entry
        self._on_result = on_result

    def emit(self, handle, view, *, final: bool = False):
        if self._entry.final:
            return {"ok": False, "error": "entry_terminal"}
        text = _text_for(view)
        try:
            result = self._entry.message.edit(text) if self._entry.message else None
        except Exception as exc:  # a transport failure must not kill the poller
            self._on_result(self._entry, {"ok": False, "error": type(exc).__name__})
            return {"ok": False, "error": type(exc).__name__}
        if result is None:
            self._on_result(self._entry, {"ok": False, "error": "no_live_handle"})
            return {"ok": False, "error": "no_live_handle"}
        self._on_result(self._entry, result)
        if final:
            self._entry.final = True
        return result


def _registry():
    """The host's process registry, when this runtime has one (evidence, never a guess)."""
    try:
        from tools.process_registry import process_registry  # type: ignore
        return process_registry
    except Exception:  # host surface absent in an isolated runtime
        return None


def _reconcile(ledger, task_id: str) -> None:
    """Persist the *real* exit the host registry observed; silence proves nothing.

    Without ``notify_on_complete`` nothing else closes the ledger row, so a finished job
    would otherwise stay ``running`` until a sweep could only call it ``lost``. The
    registry is the evidence: it either has an exit code or it does not.
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
    """Resolve the recorded route back to a conversation (restart resume only)."""
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


def _resume_edit(ctx, task_id: str, session_key: str, message_id: str, text: str) -> dict:
    """Edit the recorded message after a restart; never posts a second entry."""
    from gateway import plugin_messages
    target, error = _resolve_target(ctx, session_key)
    if target is None:
        return {"ok": False, "error": error or "no_conversation"}
    return _run_on_loop(plugin_messages.edit(target, message_id, text, plugin_id=ctx.plugin_id))


def register(ctx) -> None:
    global _CTX
    settings = _settings(ctx)
    if not settings["enabled"]:
        return
    modules = _engine_modules(settings["engine_root"])
    store = _store(ctx, modules, settings)
    ledger = modules["liveness"].LivenessLedger(settings["liveness_db"])
    emitter_cls = modules["card_emit"].CardEmitter
    progress = modules["task_progress"]
    state = getattr(ctx, "state", None)
    routes = {}  # task_id -> session_key, persisted in plugin state for restart resume
    if state is not None:
        routes = dict(state.get("routes", {}) or {})
    entries: dict[str, _Entry] = {}
    lock = threading.Lock()
    stop = threading.Event()

    def _record_result(entry: _Entry, result: dict) -> None:
        ok = bool((result or {}).get("ok"))
        if not ok:
            entry.errors += 1
        if state is not None:
            try:
                state.set("last_delivery", {
                    "task_id": entry.task_id, "ok": ok,
                    "error": (result or {}).get("error"),
                    "edited": bool((result or {}).get("edited")),
                    "at": time.time(),
                })
            except Exception:
                logger.debug("task-progress: delivery record failed", exc_info=True)

    def _open(payload: dict) -> None:
        task_id = _background_id(payload)
        if not task_id or task_id in entries:
            return
        with lock:
            if len(entries) >= int(settings["max_tracked"]):
                logger.debug("task-progress: tracking bound reached, skipping %s", task_id)
                return
        row = ledger.get(task_id)
        snap = progress.snapshot(row, now=time.time()) if row else None
        text = progress.render(snap or {"task_id": task_id}, title="") if snap else \
            "**任务**\n状态：已接收\n`%s`" % task_id
        message = ctx.post_message(text)
        if message is None or not getattr(message, "ok", False):
            logger.info("task-progress: no conversation for %s (%s)", task_id,
                        getattr(message, "error", None))
            return
        entry = _Entry(task_id, message)
        transport = _Transport(entry, on_result=_record_result)
        try:
            store.bind(task_id, "", str(getattr(message, "message_id", "") or ""))
            entry.emitter = emitter_cls(store, task_id, transport, clock=time.time,
                                        title=snap["label"] if snap and snap.get("label") else "任务")
        except Exception:
            logger.warning("task-progress: could not bind %s", task_id, exc_info=True)
            return
        entry.snap = snap
        entry.last_emit_at = time.time()
        if snap is not None and snap.get("terminal"):
            # The process already finished before the entry opened: the first post already
            # carried the terminal text (rendered from this snapshot), so there is nothing
            # left to update and no second edit to send.
            entry.final = True
            try:
                handle = store.load(task_id)
                if handle is not None:
                    handle.state = progress.card_state(snap)
                    store.save(handle)
            except Exception:
                logger.debug("task-progress: terminal-at-open save failed", exc_info=True)
        with lock:
            entries[task_id] = entry
        session_key = ""
        try:
            from gateway.session_context import get_session_env
            session_key = get_session_env("HERMES_SESSION_KEY", "") or ""
        except Exception:
            session_key = ""
        if state is not None and session_key:
            routes[task_id] = session_key
            state.set("routes", routes)

    def _poll_once() -> None:
        now = time.time()
        with lock:
            current = list(entries.items())
        for task_id, entry in current:
            if entry.final:
                continue
            _reconcile(ledger, task_id)
            row = ledger.get(task_id)
            snap = progress.snapshot(row, now=now)
            if not progress.should_emit(entry.snap, snap, last_emit_at=entry.last_emit_at,
                                        now=now, min_interval=float(settings["min_interval"])):
                continue
            entry.snap = snap
            entry.last_emit_at = now
            try:
                if snap["terminal"]:
                    verdict = snap["verdict"]
                    if verdict == "done":
                        entry.emitter.finish(phase=snap["phase"], artifacts=snap["artifacts"])
                    elif verdict == "cancelled":
                        entry.emitter.cancel(detail=snap["detail"])
                    else:
                        entry.emitter.fail(detail=snap["detail"] or verdict)
                else:
                    entry.emitter.update(phase=snap["phase"], detail=snap["detail"],
                                         artifacts=snap["artifacts"])
            except Exception:
                entry.errors += 1
                logger.warning("task-progress: emit failed for %s", task_id, exc_info=True)

    def _resume() -> None:
        """After a restart: edit the recorded entry once instead of opening a new one."""
        if not settings["resume_after_restart"]:
            return
        for handle in store.active()[:_DEFAULT_RECONCILE_LIMIT]:
            session_key = routes.get(handle.task_id, "")
            if not session_key or not handle.message_id:
                continue
            row = ledger.get(handle.task_id)
            snap = progress.snapshot(row, now=time.time())
            text = progress.render(snap, title=snap.get("label") or "任务")
            try:
                result = _resume_edit(ctx, handle.task_id, session_key, handle.message_id, text)
            except Exception as error:
                result = {"ok": False, "error": type(error).__name__}
            if not (result or {}).get("ok"):
                if state is not None:
                    try:
                        state.set("entry_frozen", {"task_id": handle.task_id,
                                                   "error": (result or {}).get("error"),
                                                   "at": time.time()})
                    except Exception:
                        logger.debug("task-progress: freeze record failed", exc_info=True)
                continue
            if snap["terminal"]:
                handle.state = progress.card_state(snap)
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
                           session_id: str = "", **_: Any) -> None:
        if tool_name != "terminal":
            return None
        payload = _parse_result(result)
        if payload is None:
            return None
        try:
            _open(payload)
        except Exception:
            logger.warning("task-progress: open failed", exc_info=True)
        return None

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
