"""Prefer-C1 thin HITL bridge: waiting_user / plan cards → Approve / Deny+reason.

Stacks on the existing single CardWriter + pool HITL surface. No new model turn,
no gateway ``input()``, no second card protocol. Source labels come from
``job.origin`` and CardHandle colleague fields only — never model-forged.

Interactive Feishu buttons are preferred; when Card Kit cannot host them the
text-fallback path still records Approve / Deny with reason (deliver-or-reason).
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Callable, Dict, Optional

#: Deny reason length — rails ≤160–240; enforce non-empty and hard cap.
DENY_REASON_MAX = 240
DENY_REASON_MIN = 1

#: Text-fallback command shapes (same card chat; no gateway input() block).
_APPROVE_RE = re.compile(r"^\s*(APPROVE|批准|同意)\s*$", re.IGNORECASE)
_DENY_RE = re.compile(
    r"^\s*(DENY|拒绝|否决)\s*[:：]\s*(.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)


def labels_from_origin(origin: Optional[dict], handle=None) -> Dict[str, str]:
    """Host-only approval source labels. Blank-ok; never invent from model text.

    * ``from_bot`` — initiating bot / primary handle (origin.profile, else session)
    * ``source_thread`` — chat/thread/turn cursor from job origin
    * ``role_id`` / ``display_name`` — waiting colleague from CardHandle ontology
    """
    from_bot = ""
    source_thread = ""
    if isinstance(origin, dict):
        profile = origin.get("profile")
        session = origin.get("session")
        if isinstance(profile, str) and profile.strip():
            from_bot = profile.strip()[:80]
        elif isinstance(session, str) and session.strip():
            from_bot = session.strip()[:80]
        parts = []
        for key in ("chat", "thread", "turn"):
            value = origin.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip()[:128])
        if parts:
            source_thread = "/".join(parts)[:160]
    role_id = ""
    display_name = ""
    if handle is not None:
        role_id = str(getattr(handle, "role_id", "") or "")[:64]
        display_name = str(getattr(handle, "display_name", "") or "")[:80]
    return {
        "from_bot": from_bot,
        "source_thread": source_thread,
        "role_id": role_id,
        "display_name": display_name,
    }


def normalize_deny_reason(reason: Any) -> str:
    """Require a short non-empty deny reason; raise if missing."""
    if reason is None:
        raise ValueError("deny_reason_required")
    text = str(reason).strip()
    if len(text) < DENY_REASON_MIN:
        raise ValueError("deny_reason_required")
    return text[:DENY_REASON_MAX]


def parse_text_fallback_command(text: str) -> Optional[Dict[str, str]]:
    """Parse APPROVE / DENY:<reason> from a chat reply (text-fallback path)."""
    if not isinstance(text, str):
        return None
    if _APPROVE_RE.match(text):
        return {"action": "approve", "reason": ""}
    match = _DENY_RE.match(text)
    if match:
        return {"action": "deny", "reason": match.group(2).strip()}
    return None


class HitlBridge:
    """Correlate HITL card actions to pool resume / deny + durable events.

    ``store`` is a :class:`CardStore` (or compatible) that can park pending HITL
    correlation. ``pool`` must expose ``origin``, ``wait_for_user``,
    ``await_plan_approval``, ``resume_from_user``, ``deny_from_user``.
    """

    def __init__(self, pool, store, *, clock: Optional[Callable[[], float]] = None):
        self.pool = pool
        self.store = store
        self._clock = clock or time.time

    def _labels(self, job: str, handle=None) -> Dict[str, str]:
        origin = None
        try:
            origin = self.pool.origin(job)
        except Exception:
            origin = None
        if handle is None and self.store is not None and hasattr(self.store, "load"):
            try:
                handle = self.store.load(job)
            except Exception:
                handle = None
        return labels_from_origin(origin, handle)

    def park_pending(self, job: str, *, kind: str, summary: str = "",
                     labels: Optional[dict] = None) -> str:
        """Persist pending HITL correlation id on the card store (thin)."""
        request_id = uuid.uuid4().hex
        labels = labels or self._labels(job)
        if self.store is not None and hasattr(self.store, "park_hitl"):
            self.store.park_hitl(
                job,
                request_id=request_id,
                kind=kind,
                summary=summary or "",
                from_bot=labels.get("from_bot", ""),
                source_thread=labels.get("source_thread", ""),
                created=self._clock(),
            )
        return request_id

    def emit_via(self, emitter, *, kind: str, detail: str = "",
                 job: str = "") -> object:
        """Emit HITL frame through the existing CardEmitter (same CardWriter).

        ``kind`` is ``waiting_user`` or ``plan``. Source labels are host-bound.
        """
        handle = getattr(emitter, "handle", None)
        labels = self._labels(job, handle) if job else labels_from_origin(None, handle)
        summary = (detail or "").strip()[:500]
        hitl_kind = "plan" if kind == "plan" else "waiting_user"
        request_id = ""
        if job:
            request_id = self.park_pending(job, kind=hitl_kind, summary=summary,
                                           labels=labels)
        kwargs = dict(
            detail=summary,
            from_bot=labels.get("from_bot", ""),
            source_thread=labels.get("source_thread", ""),
            hitl_kind=hitl_kind,
            hitl_summary=summary,
            hitl_request_id=request_id,
        )
        if hitl_kind == "plan":
            if hasattr(emitter, "await_plan"):
                return emitter.await_plan(**kwargs)
            raise TypeError("emitter_missing_await_plan")
        if hasattr(emitter, "wait_user"):
            return emitter.wait_user(**kwargs)
        raise TypeError("emitter_missing_wait_user")

    def enter_waiting_user(self, job: str, *, reason: str = "waiting_user",
                           emitter=None, detail: str = "") -> Dict[str, Any]:
        """Park pool as waiting_user then emit HITL card (if emitter bound)."""
        ok = self.pool.wait_for_user(job, reason=reason)
        view = None
        if ok:
            summary = detail or reason
            if emitter is not None:
                view = self.emit_via(emitter, kind="waiting_user",
                                     detail=summary, job=job)
            else:
                self.park_pending(job, kind="waiting_user", summary=summary)
        return {"ok": bool(ok), "kind": "waiting_user", "view": view}

    def enter_plan_approval(self, job: str, *, emitter=None,
                            detail: str = "") -> Dict[str, Any]:
        """Park pool as awaiting_plan_approval then emit plan HITL card."""
        ok = self.pool.await_plan_approval(job)
        view = None
        if ok:
            summary = detail or "awaiting_plan_approval"
            if emitter is not None:
                view = self.emit_via(emitter, kind="plan",
                                     detail=summary, job=job)
            else:
                self.park_pending(job, kind="plan", summary=summary)
        return {"ok": bool(ok), "kind": "plan", "view": view}

    def handle_action(self, *, task_id: str, action: str, reason: str = "",
                      request_id: str = "") -> Dict[str, Any]:
        """Host callback entry: Approve → resume; Deny → require reason.

        Durable events are recorded on the card store when available. Gateway
        must not block on ``input()`` — callers supply ``reason`` from the card
        form / text-fallback reply.
        """
        act = (action or "").strip().lower()
        if act in ("approve", "approve_plan", "批准", "同意"):
            return self._approve(task_id, request_id=request_id)
        if act in ("deny", "拒绝", "否决"):
            return self._deny(task_id, reason=reason, request_id=request_id)
        return {"ok": False, "error": "unknown_hitl_action", "action": action}

    def handle_text_fallback(self, *, task_id: str, text: str) -> Dict[str, Any]:
        """Text-fallback Approve/Deny path when interactive Kit buttons unavailable."""
        parsed = parse_text_fallback_command(text)
        if parsed is None:
            return {"ok": False, "error": "hitl_text_unrecognized"}
        return self.handle_action(
            task_id=task_id, action=parsed["action"], reason=parsed.get("reason", ""))

    def _approve(self, task_id: str, *, request_id: str = "") -> Dict[str, Any]:
        ok = self.pool.resume_from_user(task_id)
        event = {
            "event": "hitl_approved",
            "task_id": task_id,
            "request_id": request_id or "",
            "at": self._clock(),
        }
        if self.store is not None and hasattr(self.store, "resolve_hitl"):
            self.store.resolve_hitl(task_id, status="approved", event=event,
                                    request_id=request_id or None)
        return {"ok": bool(ok), "action": "approve", "event": event,
                "resumed": bool(ok)}

    def _deny(self, task_id: str, *, reason: str = "",
              request_id: str = "") -> Dict[str, Any]:
        try:
            normalized = normalize_deny_reason(reason)
        except ValueError as err:
            return {"ok": False, "error": str(err), "action": "deny"}
        ok = self.pool.deny_from_user(task_id, reason=normalized)
        event = {
            "event": "hitl_denied",
            "task_id": task_id,
            "request_id": request_id or "",
            "reason": normalized,
            "at": self._clock(),
        }
        if self.store is not None and hasattr(self.store, "resolve_hitl"):
            self.store.resolve_hitl(task_id, status="denied", event=event,
                                    request_id=request_id or None)
        return {"ok": bool(ok), "action": "deny", "event": event,
                "denied": bool(ok), "reason": normalized}


def hitl_text_fallback_instructions(*, task_id: str = "") -> str:
    """Honest text-fallback copy when interactive Approve/Deny cannot render."""
    tip = "回复 APPROVE 批准，或 DENY: <原因> 拒绝（原因必填，≤240字）"
    if task_id:
        return "%s · task=%s" % (tip, task_id[:16])
    return tip


def callback_value(*, task_id: str, action: str, hitl_kind: str = "",
                   request_id: str = "") -> dict:
    """Platform-neutral button value for Feishu card.action callbacks."""
    payload = {
        "action": action,
        "task_id": task_id,
        "hitl": True,
    }
    if hitl_kind:
        payload["hitl_kind"] = hitl_kind
    if request_id:
        payload["request_id"] = request_id
    return payload


def decode_callback_value(raw) -> Optional[Dict[str, Any]]:
    """Accept dict or JSON string from Feishu card.action; ignore non-HITL."""
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    if not isinstance(data, dict):
        return None
    if not data.get("hitl") and data.get("action") not in (
            "approve", "deny", "approve_plan"):
        return None
    task_id = data.get("task_id")
    action = data.get("action")
    if not isinstance(task_id, str) or not task_id.strip():
        return None
    if not isinstance(action, str) or not action.strip():
        return None
    reason = data.get("reason") or data.get("deny_reason") or ""
    if not isinstance(reason, str):
        reason = str(reason) if reason is not None else ""
    return {
        "task_id": task_id.strip(),
        "action": action.strip(),
        "reason": reason,
        "request_id": str(data.get("request_id") or ""),
        "hitl_kind": str(data.get("hitl_kind") or ""),
    }
