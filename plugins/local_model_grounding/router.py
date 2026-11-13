"""Frontdesk pre-routing workflow backed by deterministic rules and decider-4b.

Reads the role and query-intent catalog dynamically from a ``roles_spec.json``
file (Single Source of Truth) so no domain-specific role names are hardcoded in
the plugin. Fail-open by design: low-confidence verdicts or gateway failures
return ``None`` and leave the turn to the main frontdesk model.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .client import MIN_CONFIDENCE_FLOOR, LocalModelClient

ROUTING_QUESTION = "这条用户消息最适合由哪个目标处理？"

_CANCEL_RE = re.compile(r"^\s*(取消|取消任务|取消这单|停止任务|算了别跑了)[。！!?\s]*$")
_ROSTER_RE = re.compile(r"(同事.*忙碌|忙碌情况|有哪些同事|同事列表|谁在线)")
_STATUS_RE = re.compile(r"(任务进度|跑完没|看下.*进度|查任务|后台任务状态)")

_DEFAULT_SELF_OPTION = "self: 前台直接回答或轻量查询(自我介绍/能力说明/通用知识问答/查进度/取消任务/闲聊)"
_DEFAULT_MULTI_OPTION = "multi: 需要拆分给多个后台专家协同完成"


@dataclass
class RouteCatalog:
    """Loaded routing targets and metadata from roles_spec.json."""

    options: List[str] = field(default_factory=list)
    key_to_option: Dict[str, str] = field(default_factory=dict)
    display_names: Dict[str, str] = field(default_factory=dict)
    query_actions: Dict[str, str] = field(default_factory=dict)
    mention_aliases: Dict[str, str] = field(default_factory=dict)
    mtime: float = 0.0


def _build_catalog_from_spec(spec: Dict[str, Any], mtime: float = 0.0) -> RouteCatalog:
    options: List[str] = [_DEFAULT_SELF_OPTION]
    key_to_option: Dict[str, str] = {"self": _DEFAULT_SELF_OPTION}
    display_names: Dict[str, str] = {}
    query_actions: Dict[str, str] = {}
    mention_aliases: Dict[str, str] = {}

    frontdesk = spec.get("frontdesk") if isinstance(spec.get("frontdesk"), dict) else {}
    q_intents = frontdesk.get("query_intents") if isinstance(frontdesk.get("query_intents"), dict) else {}
    for intent_id, meta in q_intents.items():
        if not isinstance(meta, dict):
            continue
        key = f"query:{intent_id}"
        desc = str(meta.get("description") or intent_id).strip()
        opt = f"{key}: 轻量只读查询({desc})"
        options.append(opt)
        key_to_option[key] = opt
        action = str(meta.get("action") or "").strip()
        if action:
            query_actions[key] = action

    roles = spec.get("roles") if isinstance(spec.get("roles"), dict) else {}
    for role_id, meta in roles.items():
        if not isinstance(meta, dict):
            continue
        r_key = str(role_id).strip()
        if not r_key:
            continue
        dname = str(meta.get("display_name") or r_key).strip()
        duty = str(meta.get("duty") or "").strip()
        can_do = meta.get("can_do") if isinstance(meta.get("can_do"), list) else []
        summary = duty[:60] if duty else ", ".join(str(x) for x in can_do[:3])
        opt = f"{r_key}: {dname}({summary})" if summary else f"{r_key}: {dname}"
        options.append(opt)
        key_to_option[r_key] = opt
        display_names[r_key] = dname
        mention_aliases[f"@{r_key.lower()}"] = r_key
        if dname:
            mention_aliases[f"@{dname.lower()}"] = r_key
            short_name = dname.split("(")[0].split("（")[0].strip()
            if short_name:
                mention_aliases[f"@{short_name.lower()}"] = r_key
            nick = re.search(r"[（(]([^）)]+)[）)]", dname)
            if nick and nick.group(1).strip():
                mention_aliases[f"@{nick.group(1).strip().lower()}"] = r_key

    options.append(_DEFAULT_MULTI_OPTION)
    key_to_option["multi"] = _DEFAULT_MULTI_OPTION
    return RouteCatalog(
        options=options,
        key_to_option=key_to_option,
        display_names=display_names,
        query_actions=query_actions,
        mention_aliases=mention_aliases,
        mtime=mtime,
    )


def _msg_text(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        content = item.get("content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [p.get("text", "") for p in content if isinstance(p, dict) and isinstance(p.get("text"), str)]
            return " ".join(parts).strip()
    return ""


def build_routing_text(
    user_message: str,
    conversation_history: Optional[Sequence[Any]] = None,
    *,
    max_history_turns: int = 2,
    max_chars: int = 800,
) -> str:
    """Combine recent conversation turns with the current message for context-aware routing."""
    current = (user_message or "").strip()
    if not current:
        return ""
    snippets: List[str] = []
    if conversation_history and max_history_turns > 0:
        for item in list(conversation_history)[-(max_history_turns * 2):]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "")
            if role not in ("user", "assistant"):
                continue
            txt = _msg_text(item).replace("\n", " ")
            if not txt or txt == current:
                continue
            prefix = "用户" if role == "user" else "助手"
            snippets.append(f"{prefix}: {txt[:140]}")
        snippets = snippets[-max_history_turns:]
    if not snippets:
        return current[:max_chars]
    ctx_block = "\n".join(snippets)
    combined = f"[最近上下文]\n{ctx_block}\n[当前消息]\n{current}"
    if len(combined) > max_chars:
        combined = combined[-max_chars:]
    return combined


def fastpath_route(user_message: str, catalog: RouteCatalog) -> Optional[Dict[str, Any]]:
    """Deterministic 0ms fast-path for unambiguous commands and explicit @mentions."""
    msg = (user_message or "").strip()
    if not msg:
        return None
    if _CANCEL_RE.match(msg):
        return {"target": "self", "action": "durable_cancel", "confidence": 1.0, "source": "fastpath", "reason": "显式取消指令"}
    if _ROSTER_RE.search(msg):
        return {"target": "self", "action": "durable_roster", "confidence": 1.0, "source": "fastpath", "reason": "查询同事忙碌情况"}
    if _STATUS_RE.search(msg) and len(msg) <= 40:
        return {"target": "query:task_status", "action": "durable_status", "confidence": 1.0, "source": "fastpath", "reason": "查询后台任务进度"}
    lowered = msg.lower()
    for alias, role_id in catalog.mention_aliases.items():
        if alias in lowered:
            return {"target": role_id, "action": "durable_submit", "confidence": 1.0, "source": "fastpath", "reason": f"显式提及 {alias}"}
    return None


def format_route_hint(decision: Dict[str, Any], catalog: RouteCatalog) -> str:
    """Format a concise deterministic routing hint for pre_llm_call context injection."""
    target = str(decision.get("target") or "")
    conf = float(decision.get("confidence", 0.0) or 0.0)
    src = str(decision.get("source") or "decider")
    action = str(decision.get("action") or catalog.query_actions.get(target) or "")

    if target == "self":
        hint_action = f"优先调用 {action}" if action else "由前台直接回答，无需派发后台"
        return f"[ROUTE_HINT source={src} target=self confidence={conf:.2f}] {hint_action}。"
    if target.startswith("query:"):
        act_text = f"建议直接执行 `{action}` 并汇总结果回复" if action else "建议直接执行轻量查询并回复"
        return f"[ROUTE_HINT source={src} target={target} confidence={conf:.2f}] {act_text}，无需新建后台长任务。"
    if target == "multi":
        return f"[ROUTE_HINT source={src} target=multi confidence={conf:.2f}] 涉及多角色协同，请按职责拆分子任务依次/并行派发。"
    dname = catalog.display_names.get(target, target)
    return (
        f"[ROUTE_HINT source={src} target={target} display_name={dname} confidence={conf:.2f}] "
        f"请调用 durable_submit(role=\"{target}\", ...) 派发给 {dname} 并返回追踪卡片，禁止前台替代执行。"
    )


class FrontdeskRouter:
    """Workflow orchestrator: deterministic fast-path + decider-4b + threshold gate."""

    def __init__(
        self,
        client: LocalModelClient,
        *,
        roles_spec_path: str = "",
        min_confidence: float = 0.65,
        timeout: float = 5.0,
        max_history_turns: int = 2,
        audit_fn: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.client = client
        self.roles_spec_path = (roles_spec_path or "").strip()
        try:
            floor = float(min_confidence)
        except (TypeError, ValueError):
            floor = 0.65
        self.min_confidence = max(MIN_CONFIDENCE_FLOOR, min(1.0, floor))
        self.timeout = float(timeout or 5.0)
        self.max_history_turns = max(0, int(max_history_turns))
        self.audit_fn = audit_fn
        self._lock = threading.Lock()
        self._catalog: Optional[RouteCatalog] = None

    def get_catalog(self) -> Optional[RouteCatalog]:
        if not self.roles_spec_path:
            return None
        path = Path(self.roles_spec_path).expanduser()
        try:
            st = path.stat()
        except OSError:
            return None
        with self._lock:
            if self._catalog is not None and self._catalog.mtime == st.st_mtime:
                return self._catalog
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(spec, dict):
            return None
        catalog = _build_catalog_from_spec(spec, mtime=st.st_mtime)
        with self._lock:
            self._catalog = catalog
        return catalog

    def route(
        self,
        user_message: str,
        conversation_history: Optional[Sequence[Any]] = None,
        *,
        session_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        catalog = self.get_catalog()
        if catalog is None or len(catalog.options) < 2:
            return None

        fast = fastpath_route(user_message, catalog)
        if fast is not None:
            fast["hint"] = format_route_hint(fast, catalog)
            if self.audit_fn:
                self.audit_fn({
                    "kind": "frontdesk_route",
                    "session_id": session_id,
                    "source": "fastpath",
                    "target": fast["target"],
                    "confidence": fast["confidence"],
                    "threshold": self.min_confidence,
                    "injected": True,
                })
            return fast

        if not self.client.configured:
            return None

        text = build_routing_text(user_message, conversation_history, max_history_turns=self.max_history_turns)
        if not text:
            return None

        t0 = time.time()
        verdict = self.client.decide(text, ROUTING_QUESTION, catalog.options, timeout=self.timeout)
        if verdict is None:
            if self.audit_fn:
                self.audit_fn({
                    "kind": "frontdesk_route",
                    "session_id": session_id,
                    "source": "decider",
                    "available": False,
                    "threshold": self.min_confidence,
                    "injected": False,
                })
            return None

        choice = str(verdict.get("choice") or "").strip()
        target = next(
            (k for k, opt in catalog.key_to_option.items() if choice == opt or choice == k or choice.startswith(k + ":")),
            choice.split(":", 1)[0].strip() if ":" in choice else choice,
        )
        confidence = float(verdict.get("confidence", 0.0) or 0.0)
        decisive = confidence >= self.min_confidence and target in catalog.key_to_option
        elapsed_ms = float(verdict.get("elapsed_ms", 0.0) or round((time.time() - t0) * 1000.0, 1))

        result = {
            "target": target,
            "choice": choice,
            "confidence": confidence,
            "decisive": decisive,
            "elapsed_ms": elapsed_ms,
            "source": "decider",
        }
        if decisive:
            result["hint"] = format_route_hint(result, catalog)
        if self.audit_fn:
            self.audit_fn({
                "kind": "frontdesk_route",
                "session_id": session_id,
                "source": "decider",
                "available": True,
                "target": target,
                "confidence": round(confidence, 4),
                "threshold": self.min_confidence,
                "elapsed_ms": elapsed_ms,
                "injected": decisive,
            })
        return result if decisive else None
