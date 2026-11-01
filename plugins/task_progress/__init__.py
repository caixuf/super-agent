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
import concurrent.futures
import importlib
import importlib.util
import hashlib
import json
import logging
import sys
import threading
import time
import types
import weakref
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_CTX = None
_ENGINE = ("liveness", "task_card", "card_store", "task_progress")
_DEFAULT_POLL_SECONDS = 10.0
_DEFAULT_MIN_INTERVAL = 60.0
_DEFAULT_MAX_TRACKED = 20
_DEFAULT_RECONCILE_LIMIT = 20


_EDIT_LOCKS = weakref.WeakKeyDictionary()
_EDIT_LOCKS_GUARD = threading.Lock()
#: 每个事件循环保留的编辑锁条目上限。锁本身很小，但 key 含 message_id，
#: 长跑网关会为**每条**消息留一把锁，从不回收 —— 上限是唯一兜底。
_MAX_EDIT_LOCKS = 512

#: Reader-facing word for a ledger verdict.
#:
#: Kept here rather than imported from ``agentcore.task_progress`` on purpose:
#: the engine package is a plain module, so a plugin hot-reload re-executes this
#: file while ``agentcore`` stays the copy the gateway imported at boot. Calling
#: a helper that only exists in the *new* engine code would raise AttributeError
#: in the running gateway until it restarts. Two small tables beat a live crash;
#: ``tests/test_task_progress.py`` keeps them identical.
_VERDICT_WORD = {
    "queued": "已接收",
    "running": "进行中",
    "unverified": "待验收",
    "done": "已完成",
    "failed": "未完成",
    "cancelled": "已取消",
    "stalled": "无响应",
    "lost": "进程已不在",
    "waiting_user": "等待用户",
    "awaiting_plan_approval": "等待计划审批",
}


def _verdict_word(verdict: Any) -> str:
    return _VERDICT_WORD.get(str(verdict or ""), str(verdict or ""))


class _EditOutcomeUnknown(Exception):
    pass


def _edit_lock_for(target, message_id):
    loop = asyncio.get_running_loop()
    key = (str(getattr(target, "platform", "") or ""),
           str(getattr(target, "chat_id", "") or ""), str(message_id or ""))
    with _EDIT_LOCKS_GUARD:
        per_loop = _EDIT_LOCKS.setdefault(loop, {})
        lock = per_loop.get(key)
        if lock is None:
            lock = asyncio.Lock()
            per_loop[key] = lock
        _trim_edit_locks_locked(per_loop)
        return lock


def _trim_edit_locks_locked(per_loop):
    """丢弃最旧的编辑锁，把表压在上限内。

    只丢**没被持有**的锁：正在等锁的协程若被换掉，等价于失去互斥，
    可能让同一条消息被改两次。调用者持有 _EDIT_LOCKS_GUARD。
    """
    if len(per_loop) <= _MAX_EDIT_LOCKS:
        return
    for key in list(per_loop):
        if len(per_loop) <= _MAX_EDIT_LOCKS:
            break
        lock = per_loop.get(key)
        if lock is not None and not lock.locked():
            per_loop.pop(key, None)


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


def _reconcile(ledger, task_id: str, *, owner: str = "") -> None:
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
        if status in ("cancelled", "interrupted", "killed"):
            ledger.finish(task_id, "cancelled", exit_code=code, tail=tail,
                          detail="host process %s" % status, require_owner=owner or None)
        elif code is not None and status not in (None, "not_found", "unknown"):
            ledger.record_exit(task_id, int(code), tail=tail, detail="host process %s" % status,
                               require_owner=owner or None)
        elif status in ("completed", "success"):
            ledger.finish(task_id, "done", tail=tail, detail="host process %s" % status,
                          require_owner=owner or None)
        elif status in ("failed", "error"):
            ledger.finish(task_id, "failed", exit_code=code, tail=tail,
                          detail="host process %s" % status, require_owner=owner or None)
    except Exception:
        logger.debug("task-progress: reconcile write failed for %s", task_id, exc_info=True)


# --------------------------------------------------------------------------- #
# Per-entry bookkeeping
# --------------------------------------------------------------------------- #

class _Entry:
    """One editable entry shared by a foreground turn and its background tasks."""

    __slots__ = ("key", "message", "writer", "tasks", "foreground", "session_key",
                 "owner", "snap", "last_emit_at", "final", "frozen", "edit_state",
                 "terminal_pending")

    def __init__(self, key: str, message: Any, tasks, *, foreground=None, session_key="", owner=""):
        self.key = key
        self.message = message
        self.writer = None
        self.tasks = list(tasks)
        self.foreground = dict(foreground) if isinstance(foreground, dict) else None
        self.session_key = session_key
        self.owner = owner
        self.snap = None
        self.last_emit_at = 0.0
        self.final = False
        self.frozen = False
        self.edit_state = "confirmed"
        #: Set by the terminal lifecycle hook so the next poll bypasses the throttle.
        self.terminal_pending = False


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


def _message_transport():
    try:
        return importlib.import_module('gateway.plugin_messages')
    except ModuleNotFoundError as exc:
        if exc.name != 'gateway.plugin_messages':
            raise
    name = '_super_agent_task_progress_legacy_messages'
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name('legacy_messages.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[name] = module
    return sys.modules[name]


def _resolve_target(ctx, session_key: str):
    plugin_messages = _message_transport()
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


def _run_edit_on_loop(coro, timeout: float = 15.0):
    from gateway.run import _gateway_runner_ref
    runner = _gateway_runner_ref()
    loop = getattr(runner, "_gateway_loop", None) if runner is not None else None
    if loop is None or loop.is_closed():
        coro.close()
        raise RuntimeError("gateway loop unavailable")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(timeout)
    except concurrent.futures.TimeoutError:
        # The coroutine may still reach the adapter after this caller times out.
        # Do not cancel/replace it; callers persist unknown and freeze this message.
        raise _EditOutcomeUnknown() from None


async def _edit_only_unlocked(target, message_id: str, text: str, *, plugin_id: str) -> dict:
    """Edit one existing message without the host API's send-on-edit fallback."""
    plugin_messages = _message_transport()
    if not isinstance(message_id, str) or not message_id:
        return {"ok": False, "error": "missing_message_id"}
    if target.live is not None:
        # resolve() generates a fresh key each time; resume must publish on the
        # original sticky-notification key so local clients replace it in place.
        target.notice_key = message_id
        result = plugin_messages._publish_local(target, text)
        return {**result, "edited": bool(result.get("ok"))}
    adapter = target.adapter
    if adapter is None or not plugin_messages._supports_edit(adapter):
        return {"ok": False, "error": "edit_unsupported"}
    content = plugin_messages._fit(adapter, target.chat_id, text)
    adapter_module = sys.modules.get(type(adapter).__module__)
    platform_name = str(getattr(target, "platform", "") or "").strip().lower()
    module_file = getattr(adapter_module, "__file__", "") if adapter_module else ""
    try:
        module_path = Path(module_file).resolve().as_posix()
    except (OSError, TypeError, ValueError):
        module_path = ""
    official_feishu_module = module_path.endswith("/plugins/platforms/feishu/adapter.py")
    official_feishu_type = (type(adapter).__name__ == "FeishuAdapter" and official_feishu_module)
    feishu_route = platform_name in {"feishu", "lark", "feishu-platform", "lark-platform"}
    if not feishu_route or not official_feishu_type:
        # No other adapter edit contract has been audited for hidden send/split fallbacks.
        return {"ok": False, "error": "edit_unsupported"}
    # Verified against Hermes' FeishuAdapter source: unsupported tracked cards
    # call _feishu_send_with_retry. Check card state and renderer before entering
    # edit_message, including dynamically-loaded hermes_plugins namespaces.
    is_card = getattr(adapter, "_is_card_message", None)
    renders = getattr(adapter_module, "card_renders_markdown", None)
    if not callable(is_card) or not callable(renders):
        return {"ok": False, "error": "edit_unsupported"}
    try:
        card_target = bool(is_card(message_id))
        formatter = getattr(adapter, "format_message", None)
        rendered_content = formatter(content) if callable(formatter) else content
        if card_target and not renders(rendered_content):
            return {"ok": False, "error": "edit_unsupported"}
    except Exception:
        return {"ok": False, "error": "edit_unsupported"}
    try:
        sent = await adapter.edit_message(target.chat_id, message_id, content)
    except Exception as exc:
        logger.warning("task-progress: edit-only transport raised (%s)", type(exc).__name__)
        return {"ok": False, "error": "edit_outcome_unknown", "unknown": True}
    if not getattr(sent, "success", False):
        response = getattr(sent, "raw_response", None)
        succeeded = getattr(response, "success", None)
        code = getattr(response, "code", None)
        if callable(succeeded) and code is not None:
            try:
                if succeeded() is False:
                    return {"ok": False, "error": "edit_rejected", "retry_safe": True}
            except Exception:
                pass
        return {"ok": False, "error": "edit_outcome_unknown", "unknown": True}
    return {"ok": True, "message_id": str(getattr(sent, "message_id", None) or message_id),
            "edited": True}


async def _edit_only(target, message_id: str, text: str, *, plugin_id: str) -> dict:
    async with _edit_lock_for(target, message_id):
        return await _edit_only_unlocked(target, message_id, text, plugin_id=plugin_id)


def _resume_edit(ctx, session_key: str, message_id: str, text: str) -> dict:
    """Edit the recorded message after a restart; never posts a second entry."""
    target, error = _resolve_target(ctx, session_key)
    if target is None:
        return {"ok": False, "error": error or "no_conversation"}
    return _run_edit_on_loop(_edit_only(target, message_id, text, plugin_id=ctx.plugin_id))


def _capture_session_key() -> str:
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_SESSION_KEY", "") or ""
    except Exception:
        return ""


def _post_to_session(ctx, session_key: str, text: str, *, allow_bound_context=False) -> dict:
    if (not session_key or allow_bound_context) and callable(getattr(ctx, 'post_message', None)):
        # Only a synchronous lifecycle hook may use its already-bound host context.
        try:
            handle = ctx.post_message(text)
            return {"ok": bool(getattr(handle, "ok", False)),
                    "message_id": getattr(handle, "message_id", None), "handle": handle}
        except Exception:
            return {"ok": False, "error": "no_conversation"}
    plugin_messages = _message_transport()
    target, error = _resolve_target(ctx, session_key)
    if target is None:
        return {"ok": False, "error": error or "no_conversation"}
    return _run_on_loop(plugin_messages.post(target, text, plugin_id=ctx.plugin_id))


def _edit_to_session(ctx, session_key: str, message_id: str, text: str) -> dict:
    if not session_key:
        return {"ok": False, "error": "no_conversation"}
    target, error = _resolve_target(ctx, session_key)
    if target is None:
        return {"ok": False, "error": error or "no_conversation"}
    return _run_edit_on_loop(_edit_only(target, message_id, text, plugin_id=ctx.plugin_id))


class _RouteMessage:
    def __init__(self, ctx, session_key: str, message_id: str):
        self.ctx, self.session_key, self.message_id = ctx, session_key, message_id
        self.ok, self.error = True, None

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
    #: Terminal-publish workers, joined on unload so none outlives the plugin.
    _terminal_threads: list = []
    lock = threading.Lock()
    stop = threading.Event()
    wake = threading.Event()
    turns: Dict[str, dict] = {}
    opening = set()
    pending_tasks: Dict[str, list] = {}

    def _state_get(key, default):
        if state is None:
            return default
        try:
            return state.get(key, default)
        except Exception:
            return default

    def _state_set(key, value):
        if state is None:
            return False
        try:
            state.set(key, value)
            return True
        except Exception:
            logger.debug("task-progress: state write failed (%s)", key, exc_info=True)
            return False

    def _record_result(entry: _Entry, result: dict) -> None:
        ok = bool((result or {}).get("ok"))
        _state_set("last_delivery", {"entry": entry.key, "ok": ok,
                                     "error": (result or {}).get("error"),
                                     "edited": bool((result or {}).get("edited")),
                                     "at": time.time()})

    def _persist_entry(entry: _Entry) -> None:
        members = dict(_state_get("entries", {}) or {})
        record = dict(members.get(entry.key) or {})
        record.update({
            "session_key": entry.session_key,
            "owner": entry.owner,
            "edit_state": entry.edit_state,
            "tasks": list(entry.tasks),
            "foreground": dict(entry.foreground) if entry.foreground else None,
            "message_id": getattr(entry.message, "message_id", None) or record.get("message_id"),
            "pending_tasks": [],
            "send_state": "sent",
            "opened_at": record.get("opened_at", time.time()),
        })
        members[entry.key] = record
        return _state_set("entries", members)

    def _set_edit_state(key: str, edit_state: str, fallback: dict | None = None) -> bool:
        members = dict(_state_get("entries", {}) or {})
        record = dict(members.get(key) or fallback or {})
        record["edit_state"] = edit_state
        members[key] = record
        return _state_set("entries", members)

    def _rehydrate_entry(key: str, state_word: str) -> bool:
        """Rebuild an in-memory entry from its durable record, for a terminal frame.

        Returns True when the entry now exists and can be edited. A record without a
        message id cannot be edited, so it is not treated as success — announcing a
        finish we cannot render would be the same lie in a different place.
        """
        record = dict((_state_get("entries", {}) or {}).get(key) or {})
        message_id = str(record.get("message_id") or "")
        if not message_id:
            return False
        session_key = str(record.get("session_key") or "")
        owner = str(record.get("owner") or "")
        tasks = [t for t in (record.get("tasks") or []) if isinstance(t, str)]
        foreground = record.get("foreground")
        if not isinstance(foreground, dict):
            foreground = {}
        foreground = dict(foreground)
        foreground.update(state=state_word, phase="本轮已完成" if state_word == "done" else "状态待确认",
                          detail="")
        try:
            entry = _Entry(key, _RouteMessage(ctx, session_key, message_id), tasks,
                           foreground=foreground, session_key=session_key, owner=owner)
        except Exception:
            logger.debug("task-progress: cannot rehydrate %s", key, exc_info=True)
            return False
        with lock:
            existing = entries.get(key)
            if existing is not None:
                return True
            entries[key] = entry
        return True

    def _publish_terminal_now(key: str, state_word: str) -> None:
        """Publish the terminal frame on a short-lived thread.

        Two constraints meet here. The hook must return immediately — it runs on the
        host's turn-teardown path, and a slow transport there stalls the turn (its own
        test pins that). And the frame must not wait for the next poll — the loop may
        be mid-sleep, and a frozen/throttled entry would never pick it up, leaving the
        card parked on 进行中 while the ledger says done. A one-shot thread satisfies
        both: the hook returns at once, and the edit is attempted now.
        """
        try:
            entry = entries.get(key)
            if entry is None or entry.final:
                return
        except Exception:
            return

        def _run() -> None:
            try:
                with lock:
                    entry = entries.get(key)
                    if entry is None or entry.final:
                        return
                now = time.time()
                snaps = _snaps(entry)
                head, foreground = _head(entry, snaps, now)
                text = _render_entry(entry, snaps, foreground)
                result = _publish(entry, text, final=True)
                if not result.get("ok"):
                    entry.final = False
                    logger.debug("task-progress: terminal publish deferred for %s: %s",
                                 key, result.get("error"))
                    return
                entry.snap = head
                entry.last_emit_at = now
                _close_card(store, entry, card_state, progress)
            except Exception:
                logger.debug("task-progress: terminal publish failed for %s", key, exc_info=True)

        with lock:
            entry.terminal_pending = True
        worker = threading.Thread(target=_run, name="task-progress-terminal", daemon=True)
        with lock:
            _terminal_threads.append(worker)
        worker.start()
        wake.set()

    def _publish(entry: _Entry, text: str, *, final: bool) -> dict:
        if entry.final:
            return {"ok": False, "error": "entry_terminal"}
        # Write-ahead intent is durable before the adapter can observe an edit.
        entry.edit_state = "sending"
        if not _persist_entry(entry):
            # No request was made, so the caller may safely try again after storage recovers.
            entry.edit_state = "confirmed"
            return {"ok": False, "error": "edit_intent_persist_failed", "retry_safe": True}
        try:
            result = entry.message.edit(text) if entry.message is not None else None
        except _EditOutcomeUnknown:
            result = {"ok": False, "error": "edit_outcome_unknown", "unknown": True}
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
            # Verify ownership before consulting the host process registry: a foreign
            # ID must not even trigger reconciliation side effects.
            row = ledger.get(task_id, require_owner=entry.owner) if entry.owner else None
            if row is not None:
                _reconcile(ledger, task_id, owner=entry.owner)
                row = ledger.get(task_id, require_owner=entry.owner)
            snaps.append(progress.snapshot(row, now=time.time()) if row else
                         {"task_id": task_id, "verdict": "unknown", "state": "unknown",
                          "label": "", "phase": "", "percent": None, "elapsed": 0,
                          "detail": "", "artifacts": (), "terminal": False})
        return snaps

    def _head(entry: _Entry, snaps: list, now: float):
        foreground = (progress.foreground_snapshot(entry.foreground, now=now)
                      if entry.foreground else None)
        return progress.aggregate_with_foreground(snaps, foreground), foreground

    def _render_entry(entry: _Entry, snaps: list, foreground) -> str:
        items = list(snaps)
        if foreground is not None:
            items.insert(0, foreground)
        title = "本轮任务" if entry.foreground else _snaps_label(snaps)
        return progress.render_many(items, title=title)

    def _save_new_entry(key: str, session_key: str, text: str, tasks: list,
                        foreground: dict | None, initial_snaps: list, *, allow_bound_context=False,
                        reserved=False, owner="") -> None:
        # Serialize same-turn sends in this process and persist intent before transport.
        # If the process dies or the transport result is ambiguous, restart sees "sending"
        # and will not blindly post a duplicate.
        with lock:
            if reserved:
                if key not in opening:
                    return
            else:
                if key in opening:
                    pending = pending_tasks.setdefault(key, [])
                    pending.extend(task for task in tasks if task not in pending)
                    members = dict(_state_get("entries", {}) or {})
                    record = dict(members.get(key) or {})
                    durable_pending = list(record.get("pending_tasks") or [])
                    durable_pending.extend(task for task in tasks
                                           if task not in durable_pending and task not in (record.get("tasks") or []))
                    record["pending_tasks"] = durable_pending
                    members[key] = record
                    _state_set("entries", members)
                    return
                if key in entries or store.load(key) is not None:
                    return
                members = dict(_state_get("entries", {}) or {})
                prior = members.get(key) or {}
                if prior.get("send_state") in {"sending", "unknown", "sent"}:
                    return
                opening.add(key)
                members[key] = {"session_key": session_key, "owner": owner, "tasks": list(tasks),
                                "foreground": dict(foreground) if foreground else None,
                                "send_state": "sending", "opened_at": time.time()}
                if not _state_set("entries", members):
                    opening.discard(key)
                    logger.warning("task-progress: cannot persist send intent for %s; skipping post", key)
                    return
        try:
            _save_new_entry_once(key, session_key, text, tasks, foreground, initial_snaps,
                                 allow_bound_context=allow_bound_context, owner=owner)
        finally:
            with lock:
                opening.discard(key)
                members = dict(_state_get("entries", {}) or {})
                record = dict(members.get(key) or {})
                entry = entries.get(key)
                waiting = pending_tasks.pop(key, [])
                if entry is not None:
                    entry.tasks.extend(task for task in waiting if task not in entry.tasks)
                    if waiting:
                        # The posted card predates these tasks, so even an apparently
                        # terminal first snapshot must be refreshed before terminal lock.
                        entry.snap = None
                        entry.final = False
                    if (entry.foreground or {}).get("state") in {"done", "failed", "cancelled"}:
                        turns.pop(key, None)
                    # One state write contains the merged member list and terminal projection.
                    record.update({
                        "session_key": entry.session_key,
                        "owner": entry.owner,
                        "tasks": list(entry.tasks),
                        "pending_tasks": [],
                        "foreground": dict(entry.foreground) if entry.foreground else None,
                        "message_id": getattr(entry.message, "message_id", None) or record.get("message_id"),
                        "send_state": "sent",
                    })
                    members[key] = record
                    _state_set("entries", members)
                    if not waiting and entry.snap and entry.snap.get("terminal"):
                        entry.final = True
                        _close_card(store, entry, card_state, progress)
                elif store.load(key) is not None:
                    handle = store.load(key)
                    record["message_id"] = handle.message_id
                    record["send_state"] = "sent"
                else:
                    record["send_state"] = "posted" if record.get("message_id") else "unknown"
                    durable_pending = list(record.get("pending_tasks") or [])
                    durable_pending.extend(task for task in waiting
                                           if task not in durable_pending and task not in (record.get("tasks") or []))
                    record["pending_tasks"] = durable_pending
                members[key] = record
                _state_set("entries", members)

    def _persist_posted_message(key: str, message_id: str) -> None:
        with lock:
            members = dict(_state_get("entries", {}) or {})
            record = dict(members.get(key) or {})
            record["message_id"] = message_id
            record["send_state"] = "posted"
            members[key] = record
            _state_set("entries", members)

    def _save_new_entry_once(key: str, session_key: str, text: str, tasks: list,
                        foreground: dict | None, initial_snaps: list, *, allow_bound_context=False,
                        owner="") -> None:
        if store.load(key) is not None:
            logger.info("task-progress: existing card %s is not duplicated", key)
            return
        result = _post_to_session(ctx, session_key, text, allow_bound_context=allow_bound_context)
        if not result.get("ok"):
            logger.info("task-progress: no conversation for %s (%s)", key, result.get("error"))
            return
        message = result.get("handle") or _RouteMessage(
            ctx, session_key, str(result.get("message_id") or ""))
        message_id = str(result.get("message_id") or getattr(message, "message_id", "") or "")
        # Hermes PluginMessage.edit may send a new message when edit fails. Replace
        # that handle with our edit-only route wrapper; lightweight test transports
        # keep their own in-place fake handle.
        try:
            from hermes_cli.plugin_messages import PluginMessage
            if isinstance(message, PluginMessage):
                message = _RouteMessage(ctx, session_key, message_id)
        except ImportError:
            pass
        message.ok = True
        message.message_id = message_id
        if not message.message_id:
            logger.warning("task-progress: posted message has no durable ID; entry remains uncertain")
            return
        _persist_posted_message(key, message.message_id)
        # The send may have blocked while a terminal hook updated the in-flight turn.
        with lock:
            latest_foreground = dict(turns.get(key) or foreground or {}) or None
        entry = _Entry(key, message, tasks, foreground=latest_foreground, session_key=session_key,
                       owner=owner)
        try:
            store.bind(key, "", message.message_id)
            entry.writer = store.writer(key)
        except Exception:
            logger.warning("task-progress: could not bind %s", key, exc_info=True)
            return
        # Seed from exactly the evidence rendered in the posted card. A fresh registry
        # poll here could discover an exit and silently mark an outdated card terminal.
        posted_fg = progress.foreground_snapshot(foreground, now=time.time()) if foreground else None
        entry.snap = progress.aggregate_with_foreground(initial_snaps, posted_fg)
        entry.last_emit_at = time.time()
        with lock:
            entries[key] = entry
        _persist_entry(entry)

    def _open(payload: dict, *, turn_id: str, session_id: str) -> None:
        task_id = _background_id(payload)
        if not task_id:
            return
        # A process ID in a tool result is only a reference, not proof that the
        # process belongs to this conversation. Never project another owner's
        # label, phase, detail or artifacts onto the current route.
        if not isinstance(session_id, str) or not session_id:
            logger.debug("task-progress: refusing unscoped background task %s", task_id)
            return
        key = _entry_key(turn_id, session_id, task_id)
        session_key = _capture_session_key()
        with lock:
            foreground = dict(turns.get(key) or {}) or None
        owner = foreground.get("owner") if foreground else None
        owner = owner if isinstance(owner, str) and owner else (
            "%s:%s" % (getattr(ctx, "profile_name", "") or "local", session_id))
        row = ledger.get(task_id)
        if row is not None and row.get("owner") != owner:
            logger.warning("task-progress: refusing task owned by another session: %s", task_id)
            return
        with lock:
            existing = entries.get(key)
            if existing is not None:
                if existing.owner != owner:
                    logger.warning("task-progress: refusing task for entry with different owner: %s", task_id)
                    return
                if task_id not in existing.tasks:
                    existing.tasks.append(task_id)
                    existing.snap = None
                    _persist_entry(existing)
                return
            if len(entries) >= int(settings["max_tracked"]):
                logger.debug("task-progress: tracking bound reached, skipping %s", task_id)
                return
            foreground = dict(turns.get(key) or {}) or None
        now = time.time()
        snaps = [_first_snap(ledger, progress, task_id, owner=owner)]
        foreground_snap = progress.foreground_snapshot(foreground, now=now) if foreground else None
        items = ([foreground_snap] if foreground_snap else []) + snaps
        _save_new_entry(key, session_key, progress.render_many(
            items, title="本轮任务" if foreground else _snaps_label(snaps)), [task_id], foreground, snaps,
            allow_bound_context=True, owner=owner)

    def _open_foreground(key: str) -> None:
        with lock:
            if key in entries or key in opening or store.load(key) is not None:
                return
            members = dict(_state_get("entries", {}) or {})
            prior = members.get(key) or {}
            if prior.get("send_state") in {"sending", "unknown", "sent"}:
                return
            foreground = dict(turns.get(key) or {})
            owner = foreground.get("owner") if isinstance(foreground.get("owner"), str) else ""
            if (not foreground or not owner or not foreground.get("session_id") or foreground.get("state") != "running"
                    or len(entries) >= int(settings["max_tracked"])):
                return
            # Reserve the turn atomically with the liveness/threshold check. A terminal
            # hook after this point sees opening and updates the retained turn.
            opening.add(key)
            members[key] = {"session_key": foreground.get("session_key", ""), "owner": owner, "tasks": [],
                            "foreground": dict(foreground), "send_state": "sending",
                            "opened_at": time.time()}
            if not _state_set("entries", members):
                opening.discard(key)
                logger.warning("task-progress: cannot persist foreground intent for %s; skipping post", key)
                return
        snap = progress.foreground_snapshot(foreground, now=time.time())
        _save_new_entry(key, foreground.get("session_key", ""),
                        progress.render_many([snap], title="本轮任务"), [], foreground,
                        [], allow_bound_context=False, reserved=True, owner=owner)

    def _set_turn(key: str, *, phase: str | None = None, detail: str | None = None,
                  state: str | None = None) -> None:
        with lock:
            turn = turns.get(key)
            entry = entries.get(key)
            if turn is None and entry is not None:
                turn = dict(entry.foreground or {})
                turns[key] = turn
            if turn is None:
                return
            if phase is not None:
                turn["phase"] = phase
            if detail is not None:
                turn["detail"] = detail
            if state is not None:
                turn["state"] = state
                turn["terminal_at"] = time.time() if state in ("done", "failed", "cancelled") else None
            turns[key] = turn
            if entry is not None:
                entry.foreground = dict(turn)
                entry.snap = None
                _persist_entry(entry)

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
            pending = [(key, dict(turn)) for key, turn in turns.items()
                       if key not in entries
                       and now - float(turn.get("started_at") or now)
                       >= max(0.0, float(settings["foreground_min_seconds"]))]
            current = list(entries.items())
        for key, _turn in pending:
            _open_foreground(key)
        with lock:
            current = list(entries.items())
        for key, entry in current:
            if entry.final or entry.frozen:
                continue
            snaps = _snaps(entry)
            head, foreground = _head(entry, snaps, now)
            # A terminal frame requested by the lifecycle hook must not be dropped by
            # the throttle: waiting out min_interval here is what left cards spinning.
            forced = entry.terminal_pending
            if not forced and not progress.should_emit(entry.snap, head, last_emit_at=entry.last_emit_at,
                                                       now=now, min_interval=float(settings["min_interval"])):
                continue
            text = _render_entry(entry, snaps, foreground)
            final = bool(head.get("terminal"))
            result = _publish(entry, text, final=final)
            if not result.get("ok"):
                entry.final = False
                if result.get("retry_safe") is True:
                    # A concrete rejection (or a failed pre-send intent write) proves
                    # no edit was committed. Clear the write-ahead intent before retry.
                    entry.edit_state = "confirmed"
                    if not _persist_entry(entry):
                        entry.edit_state = "sending"
                        entry.frozen = True
                    continue
                entry.frozen = True
                entry.edit_state = "unknown" if result.get("unknown") is True else "frozen"
                _persist_entry(entry)
                _state_set("entry_frozen", {"entry": entry.key, "error": result.get("error"),
                                            "at": time.time()})
                continue
            entry.edit_state = "confirmed"
            if not _persist_entry(entry):
                # The remote edit succeeded, but durable confirmation did not.
                # Keep a sending/frozen record so restart cannot replay blindly.
                entry.edit_state = "sending"
                entry.frozen = True
                _state_set("entry_frozen", {"entry": entry.key,
                                            "error": "edit_confirmation_persist_failed",
                                            "at": time.time()})
                continue
            entry.snap = head
            entry.last_emit_at = now
            entry.terminal_pending = False
            if final:
                _close_card(store, entry, card_state, progress)

    def _resume() -> None:
        """After a restart: edit the recorded entry once instead of opening a new one."""
        if not settings["resume_after_restart"]:
            return
        members = dict(_state_get("entries", {}) or {})
        for key, record in members.items():
            if not isinstance(key, str) or not isinstance(record, dict):
                continue
            if record.get("edit_state") in {"sending", "unknown", "frozen"}:
                # A crash may have happened after dispatch but before confirmation.
                # Treat the durable intent as unknown and never issue it again.
                if record.get("edit_state") == "sending":
                    record["edit_state"] = "unknown"
                    members[key] = record
                    _state_set("entries", members)
                _state_set("entry_frozen", {"entry": key, "error": "edit_outcome_unknown",
                                            "at": time.time()})
                continue
            message_id = record.get("message_id")
            if not isinstance(message_id, str) or not message_id:
                if record.get("send_state") in {"sending", "unknown"} and store.load(key) is None:
                    _state_set("entry_frozen", {"entry": key,
                                                  "error": "send_outcome_unknown_no_message_id",
                                                  "at": time.time()})
                continue
            if store.load(key) is None:
                try:
                    store.bind(key, str(record.get("chat_id") or ""), message_id)
                except Exception:
                    logger.debug("task-progress: recovering card binding failed for %s", key,
                                 exc_info=True)
        for handle in store.active()[:_DEFAULT_RECONCILE_LIMIT]:
            record = members.get(handle.task_id) or {}
            if record.get("edit_state") in {"sending", "unknown", "frozen"}:
                continue
            tasks = list(dict.fromkeys(
                t for t in list(record.get("tasks") or []) + list(record.get("pending_tasks") or [])
                if isinstance(t, str)))
            session_key = record.get("session_key") or ""
            owner = record.get("owner") if isinstance(record.get("owner"), str) else ""
            snaps = [_first_snap(ledger, progress, task, owner=owner) for task in tasks]
            foreground = record.get("foreground")
            if isinstance(foreground, dict) and foreground.get("state") == "running":
                foreground = dict(foreground)
                foreground.update(state="unknown", phase="状态待确认",
                                  detail="宿主重启前未收到本回合终态")
            fg_snap = progress.foreground_snapshot(foreground, now=time.time()) if foreground else None
            head = progress.aggregate_with_foreground(snaps, fg_snap)
            if not session_key or not handle.message_id:
                if head.get("terminal"):
                    handle.state = progress.card_state({"state": head.get("verdict")})
                    store.save(handle)
                continue
            items = ([fg_snap] if fg_snap else []) + snaps
            text = progress.render_many(items, title="本轮任务" if fg_snap else _snaps_label(snaps))
            fallback = dict(record)
            fallback.update(session_key=session_key, owner=owner, tasks=tasks,
                            foreground=dict(foreground) if isinstance(foreground, dict) else None,
                            message_id=handle.message_id, send_state="sent")
            if not _set_edit_state(handle.task_id, "sending", fallback=fallback):
                _state_set("entry_frozen", {"entry": handle.task_id,
                                            "error": "edit_intent_persist_failed", "at": time.time()})
                continue
            try:
                result = _resume_edit(ctx, session_key, handle.message_id, text)
            except _EditOutcomeUnknown:
                result = {"ok": False, "error": "edit_outcome_unknown", "unknown": True}
            except Exception as error:
                result = {"ok": False, "error": type(error).__name__, "unknown": True}
            if not result.get("ok"):
                next_state = "confirmed" if result.get("retry_safe") is True else (
                    "unknown" if result.get("unknown") is True else "frozen")
                persisted = _set_edit_state(handle.task_id, next_state, fallback=fallback)
                if not persisted and next_state == "confirmed":
                    # The adapter proved no edit occurred, but without a durable
                    # cleared intent recovery must remain frozen at the sending fence.
                    _set_edit_state(handle.task_id, "sending", fallback=fallback)
                    _state_set("entry_frozen", {"entry": handle.task_id,
                                                "error": "edit_confirmation_persist_failed",
                                                "at": time.time()})
                elif next_state != "confirmed":
                    _state_set("entry_frozen", {"entry": handle.task_id,
                                                "error": result.get("error"), "at": time.time()})
                continue
            if not _set_edit_state(handle.task_id, "confirmed", fallback=fallback):
                _state_set("entry_frozen", {"entry": handle.task_id,
                                            "error": "edit_confirmation_persist_failed", "at": time.time()})
                continue
            if head.get("terminal"):
                handle.state = progress.card_state({"state": head.get("verdict")})
                store.save(handle)
                continue

            # The startup edit only refreshes the existing card. Rehydrate a live
            # entry as well, otherwise the restarted poller would never observe
            # subsequent progress or the real terminal state.
            entry = _Entry(
                handle.task_id,
                _RouteMessage(ctx, session_key, handle.message_id),
                tasks,
                foreground=foreground if isinstance(foreground, dict) else None,
                session_key=session_key,
                owner=owner,
            )
            entry.snap = head
            entry.last_emit_at = time.time()
            entry.edit_state = "confirmed"
            try:
                entry.writer = store.writer(handle.task_id)
            except Exception:
                logger.debug("task-progress: could not restore writer for %s",
                             handle.task_id, exc_info=True)
            with lock:
                if handle.task_id not in entries:
                    entries[handle.task_id] = entry

    def _loop() -> None:
        try:
            _resume()
        except Exception:
            logger.warning("task-progress: resume pass failed", exc_info=True)
        interval = max(2.0, float(settings["poll_seconds"]))
        while not stop.is_set():
            wake.wait(interval)
            if stop.is_set():
                break
            wake.clear()
            try:
                _poll_once()
            except Exception:
                logger.warning("task-progress: poll pass failed", exc_info=True)

    def _on_pre_llm_call(session_id: str = "", turn_id: str = "", **_: Any) -> None:
        if not session_id or not turn_id:
            return None
        profile_name = getattr(ctx, "profile_name", "") or "local"
        owner = "%s:%s" % (profile_name, session_id)
        key = _entry_key(turn_id, session_id, "")
        session_key = _capture_session_key()
        if not session_key:
            logger.debug("task-progress: foreground turn lacks a bound session route")
            return None
        now = time.time()
        identity = hashlib.sha256((session_key + "\0" + session_id + "\0" + turn_id).encode()).hexdigest()[:24]
        with lock:
            turns.setdefault(key, {
                "task_id": "turn_" + identity, "label": "前台回合", "kind": "foreground",
                "session_id": session_id, "turn_id": turn_id, "session_key": session_key,
                "profile_name": profile_name, "owner": owner,
                "state": "running", "phase": "等待模型响应", "detail": "正在等待模型响应",
                "started_at": now,
            })
        return None

    def _on_pre_tool_call(tool_name: str = "", args: Any = None,
                          session_id: str = "", turn_id: str = "", **_: Any) -> None:
        if session_id and turn_id:
            _set_turn(_entry_key(turn_id, session_id, ""),
                      phase="正在执行工具", detail="等待工具结果")
        return None

    def _on_post_tool_call(tool_name: str = "", args: Any = None, result: Any = None,
                           session_id: str = "", turn_id: str = "", **kwargs: Any) -> None:
        if session_id and turn_id:
            _set_turn(_entry_key(turn_id, session_id, ""),
                      phase="工具已返回，等待模型响应", detail="模型正在处理工具结果")
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

    def _on_session_end(session_id: str = "", turn_id: str = "", completed: Any = None,
                        failed: Any = None, interrupted: Any = None, **_: Any) -> None:
        if not session_id or not turn_id:
            return None
        key = _entry_key(turn_id, session_id, "")
        # The host does not pass the same ``turn_id`` shape to every hook: the turn
        # that opened the card can key as "<sid>:<sid>:<tid>" while the terminal hook
        # arrives as "<tid>". Keying only by this hook's own turn_id then matches
        # nothing, the foreground state never leaves "running", and the card sits on
        # 运行中 forever while the ledger says done. Resolve the entry from whichever
        # source still knows about it: live memory first, then the durable record —
        # the card was posted, so the record exists even when no in-memory entry does.
        if key not in entries:
            with lock:
                candidates = list(entries.keys())
            if not candidates:
                candidates = [k for k in (_state_get("entries", {}) or {})
                              if isinstance(k, str)]
            matched = [k for k in candidates
                       if isinstance(k, str) and session_id in k
                       and k.rstrip().endswith(turn_id)]
            if not matched:
                matched = [k for k in candidates
                           if isinstance(k, str) and session_id in k]
            if matched:
                key = sorted(matched)[-1]
                logger.info(
                    "task-progress: terminal hook turn_id %r did not key an entry; "
                    "settling %s for session %s",
                    str(turn_id)[:40], key[-40:], str(session_id)[:40],
                )
        if completed is True and failed is not True and interrupted is not True:
            state_word, phase, detail = "done", "本轮已完成", ""
        elif failed is True:
            state_word, phase, detail = "failed", "本轮未完成", "宿主报告回合失败"
        elif interrupted is True:
            state_word, phase, detail = "cancelled", "本轮已中断", "宿主报告回合已中断"
        else:
            state_word, phase, detail = "unknown", "状态待确认", "宿主未提供可信终态"
        with lock:
            existed = key in entries
            pending = turns.get(key)
            is_opening = key in opening
            if is_opening and pending is not None:
                pending.update(state=state_word, phase=phase, detail=detail,
                               terminal_at=time.time())
                turns[key] = pending
        if not existed:
            # The card is posted and the durable record names its message id, but no
            # in-memory entry survived (a restart, or the foreground entry was never
            # materialized). Rebuild just enough of it to write the terminal frame —
            # otherwise the card is unreachable and stays on its last running frame.
            existed = _rehydrate_entry(key, state_word)
            if existed:
                pending = None
        if not existed and pending is None:
            return None
        if existed:
            _set_turn(key, phase=phase, detail=detail, state=state_word)
            _publish_terminal_now(key, state_word)
        with lock:
            if not is_opening:
                turns.pop(key, None)
        if existed or is_opening:
            wake.set()
        return None

    # ── 不进模型的查询入口 ────────────────────────────────────────────────
    def _session_id() -> str:
        try:
            from gateway.session_context import get_session_env
            return get_session_env("HERMES_SESSION_ID", "") or ""
        except Exception:
            return ""

    def _foreground_for_owner(owner: str) -> dict:
        # Foreground turns are tracked separately from the background ledger.
        # Copy while holding the lock and never expose another session's entry.
        visible = {}
        with lock:
            for turn in turns.values():
                if turn.get("owner") == owner and isinstance(turn.get("task_id"), str):
                    visible[turn["task_id"]] = (dict(turn), False)
            for entry in entries.values():
                foreground = entry.foreground
                if (entry.owner == owner and isinstance(foreground, dict)
                        and isinstance(foreground.get("task_id"), str)):
                    visible[foreground["task_id"]] = (dict(foreground), entry.frozen)
        return visible

    def _cmd_longtasks(raw_args: str = "") -> str:
        session = _session_id()
        if not session:
            return "无法确认当前会话，暂时不能查询长任务。"
        owner = "%s:%s" % (getattr(ctx, "profile_name", "") or "local", session)
        rows = ledger.list_tasks(owner=owner, active_only=False, limit=20)
        foreground = _foreground_for_owner(owner)
        if not rows and not foreground:
            return "本会话没有登记过的长任务。"
        lines = []
        recent_foreground = sorted(foreground.items(), key=lambda item: (
            item[1][0].get("started_at") if type(item[1][0].get("started_at")) in (int, float) else 0),
                                   reverse=True)[:20]
        for task_id, (turn, frozen) in recent_foreground:
            snap = progress.foreground_snapshot(turn, now=time.time())
            word = _verdict_word(snap["verdict"])
            minutes, seconds = divmod(int(snap["elapsed"]), 60)
            elapsed = "%d分%02d秒" % (minutes, seconds) if minutes else "%d秒" % seconds
            lines.append("· `%s` 前台回合 · %s · 已用 %s%s%s" % (
                task_id, word, elapsed, (" · " + snap["phase"]) if snap.get("phase") else "",
                " · 进度消息更新待确认" if frozen else ""))
        for row in rows:
            snap = progress.snapshot(row, now=time.time())
            word = _verdict_word(snap["verdict"])
            minutes, seconds = divmod(int(snap["elapsed"]), 60)
            elapsed = "%d分%02d秒" % (minutes, seconds) if minutes else "%d秒" % seconds
            lines.append("· `%s` %s · 已用 %s%s" % (snap["task_id"], word, elapsed,
                                                    (" · " + snap["phase"]) if snap.get("phase") else ""))
        return "本会话的长任务（%d）：\n%s" % (len(lines), "\n".join(lines))

    def _cmd_longtask(raw_args: str = "") -> str:
        text = (raw_args or "").strip()
        task = text.split()[0] if text else ""
        if not task:
            return "用法：/longtask <任务号>（任务号见 /longtasks）"
        session = _session_id()
        if not session:
            return "无法确认当前会话，暂时不能查询长任务。"
        owner = "%s:%s" % (getattr(ctx, "profile_name", "") or "local", session)
        foreground = _foreground_for_owner(owner).get(task)
        if foreground is not None:
            turn, frozen = foreground
            rendered = progress.render(progress.foreground_snapshot(turn, now=time.time()), title="前台回合")
            return rendered + ("\n进度消息更新待确认，请以这里的状态为准。" if frozen else "")
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

        #: Preferred verb, then two stable plugin namespaces.  The host rejects a
        #: plugin command whose name is already a built-in, so every candidate is
        #: checked with the host's own resolver before registration.  The choices
        #: are ordered (not generated): the effective command is stable across
        #: plugin reloads and host versions that do not add the same verbs.
        _VERB_CANDIDATES = {
            "status": ("status", "taskstatus", "task-progress-status"),
            "pause": ("pause", "taskpause", "task-progress-pause"),
            "cancel": ("cancel", "taskcancel", "task-progress-cancel"),
        }
        installed = {}

        def _host_owns(name: str) -> Optional[bool]:
            """Return the host's answer, or ``None`` when it cannot be inspected.

            ``PluginContext.register_command`` rejects built-ins by calling this
            same resolver.  A missing or broken resolver is not evidence that a
            plain verb is free: the host may still reject it internally.  Keep
            the unknown state separate so registration can choose a namespaced
            fallback instead of advertising a command that may not exist.
            """
            try:
                from hermes_cli.commands import resolve_command  # host module
            except Exception:  # noqa: BLE001 - host module is optional here
                return None
            try:
                return resolve_command(name) is not None
            except Exception:  # noqa: BLE001 - resolver outage must not break load
                return None

        def _register_verb(primary, handler, description, args_hint=None):
            """Register the first host-free candidate and return its exact name.

            If all candidates are built-ins, do not register a colliding command:
            the host would silently skip it and its usage text would be a lie.
            """
            candidates = _VERB_CANDIDATES[primary]
            plain_owned = _host_owns(candidates[0])
            # Unknown host state is deliberately treated as unsafe for the plain
            # verb.  The second candidate is the stable task-prefixed namespace;
            # third-party collisions there are outside this plugin's remit.
            start = 1 if plain_owned is not False else 0
            for name in candidates[start:]:
                if name != candidates[0] and _host_owns(name) is True:
                    continue
                kwargs = {"args_hint": args_hint} if args_hint else {}
                register_command(name, handler, description.replace("<verb>", name), **kwargs)
                installed[primary] = name
                return name
            logger.warning("task-progress: every alias for %s conflicts with a host command",
                           primary)
            return None

        def _verb(primary: str) -> str:
            return installed.get(primary, primary)

        # Prefer-D1 slash verbs (reliable classify path; free-text optional).
        def _cmd_status(raw_args: str = "") -> str:
            task = (raw_args or "").strip().split()[0] if (raw_args or "").strip() else ""
            if not task:
                return "用法：/%s <任务号>（或 /longtask <任务号>）" % _verb("status")
            session = _session_id()
            if not session:
                return "无法确认当前会话，暂时不能查询任务进度。"
            try:
                from agentcore.barge_in_control import BargeInController
                ctrl = BargeInController(ledger=ledger, clock=time.time)
                out = ctrl.status(task, owner="%s:%s" % (
                    getattr(ctx, "profile_name", "") or "local", session))
                return out.get("message") or str(out)
            except Exception as exc:  # noqa: BLE001
                return "status 短路径不可用：%s" % type(exc).__name__

        def _cmd_pause(raw_args: str = "") -> str:
            task = (raw_args or "").strip().split()[0] if (raw_args or "").strip() else ""
            if not task:
                return "用法：/%s <任务号>（当前未接线，不会暂停任务）" % _verb("pause")
            return ("暂停命令当前未接线：没有执行暂停，任务状态未改变。 task=%s" % task)

        def _cmd_cancel(raw_args: str = "") -> str:
            task = (raw_args or "").strip().split()[0] if (raw_args or "").strip() else ""
            if not task:
                return "用法：/%s <任务号>（当前未接线，不会取消任务）" % _verb("cancel")
            return ("取消命令当前未接线：没有执行取消，任务状态未改变。 task=%s" % task)

        _register_verb("status", _cmd_status,
                       "任务进度短路径（不启动模型回合）：/<verb> <任务号>",
                       args_hint="<任务号>")
        _register_verb("pause", _cmd_pause,
                       "暂停任务（当前未接线，不会暂停）：/<verb> <任务号>",
                       args_hint="<任务号>")
        _register_verb("cancel", _cmd_cancel,
                       "取消任务（当前未接线，不会取消）：/<verb> <任务号>",
                       args_hint="<任务号>")

    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("on_session_end", _on_session_end)
    thread = threading.Thread(target=_loop, name="task-progress-poller", daemon=True)
    thread.start()

    def _stop():
        stop.set()
        wake.set()
        thread.join(timeout=5.0)
        # Terminal-publish threads are short-lived but must not outlive the plugin:
        # a stray edit landing after unload writes to a card this instance no longer
        # owns, and in tests it lands in the next case's fake transport.
        with lock:
            stragglers = [t for t in _terminal_threads if t.is_alive()]
        for worker in stragglers:
            worker.join(timeout=2.0)

    on_unload = getattr(ctx, "on_unload", None)
    if callable(on_unload):
        try:
            on_unload(_stop)
        except TypeError:
            pass
    _CTX = ctx
    logger.debug("task-progress registered: db=%s poll=%ss", settings["liveness_db"],
                 settings["poll_seconds"])


def _first_snap(ledger, progress, task_id: str, *, owner: str = "") -> dict:
    row = ledger.get(task_id, require_owner=owner) if owner else None
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
