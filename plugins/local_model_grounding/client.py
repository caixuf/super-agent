"""HTTP client for the local model gateway: decisions and embeddings only.

The gateway (``scripts/ops/local_model_gateway.py`` on the model host) exposes
``POST /decide`` (decider-4b) and ``POST /encode`` (bge-m3). Neither endpoint
generates prose, and this client never asks for any.

Failure policy is deliberate: every method returns ``None`` instead of raising.
A grounding plugin must never turn a gateway hiccup into a broken bot turn, and
callers must be able to distinguish "no verdict" from "verdict was something".
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

#: Confidence below this is never treated as a decision. Configuration may raise
#: it, never lower it: a low-confidence verdict must not silently rewrite code.
MIN_CONFIDENCE_FLOOR = 0.6


def read_token(token: str = "", token_file: str = "") -> str:
    """Token from config, else from a 0600 file, else empty (loopback only)."""
    if isinstance(token, str) and token.strip():
        return token.strip()
    path = (token_file or "").strip()
    if not path:
        return ""
    try:
        return Path(path).expanduser().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


#: The gateway is a private endpoint on the model host (loopback or the direct
#: cable link). urllib honours ``http_proxy``/``https_proxy``, and the gateway
#: process runs behind mihomo on this profile, so an inherited proxy would send
#: <private-lan>/24 through a foreign egress. Build an explicit no-proxy opener.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def append_jsonl(path: str, row: Dict[str, Any]) -> None:
    """Append one audit row; auditing must never break the caller."""
    if not path:
        return
    try:
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, ensure_ascii=False, sort_keys=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass


class LocalModelClient:
    """Minimal, fail-closed client for one gateway base URL."""

    def __init__(self, base_url: str, *, token: str = "", token_file: str = "",
                 timeout: float = 10.0, decider_backend: str = "gateway",
                 model_name: str = "local-judge-decider4b",
                 cooldown_s: float = 60.0) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.token = read_token(token, token_file)
        self.decider_backend = (decider_backend or "gateway").strip().lower()
        self.model_name = (model_name or "local-judge-decider4b").strip()
        try:
            self.timeout = float(timeout)
        except (TypeError, ValueError):
            self.timeout = 10.0
        if self.timeout <= 0:
            self.timeout = 10.0
        try:
            self.cooldown_s = max(0.0, float(cooldown_s))
        except (TypeError, ValueError):
            self.cooldown_s = 60.0
        self._unavailable_until: float = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _post(self, path: str, payload: Dict[str, Any],
              timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        if not self.base_url:
            return None
        if self.cooldown_s > 0.0 and time.monotonic() < self._unavailable_until:
            return None
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.base_url + path, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with _OPENER.open(request, timeout=timeout or self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError:
            self._unavailable_until = 0.0
            return None
        except (urllib.error.URLError, OSError, TimeoutError, ValueError):
            if self.cooldown_s > 0.0:
                self._unavailable_until = time.monotonic() + self.cooldown_s
            return None
        self._unavailable_until = 0.0
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _decide_openai(self, text: str, question: str, options: Sequence[str], *,
                       timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """OpenAI-compatible /v1/chat/completions decider with enum-constrained JSON."""
        started = time.time()
        opt_list = [str(o) for o in options]
        keys = [o.split(":", 1)[0].strip() if ":" in o else o.strip() for o in opt_list]
        sys_prompt = (
            f"你是分类决策器。问题：{question}\n"
            "候选列表：\n" + "\n".join(f"- {o}" for o in opt_list) + "\n"
            "只输出合法 JSON：{\"target\": \"<候选key>\", \"confidence\": 0.9}"
        )
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": text},
            ],
            "temperature": 0.0,
            "max_tokens": 64,
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "route_decision",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "target": {"type": "string", "enum": keys},
                            "confidence": {"type": "number"},
                        },
                        "required": ["target", "confidence"],
                        "additionalProperties": False,
                    },
                },
            },
        }
        body = self._post("/v1/chat/completions", payload, timeout=timeout)
        if not body:
            return None
        try:
            content = (body["choices"][0]["message"].get("content") or "").strip()
            parsed = json.loads(content)
            target = str(parsed.get("target") or "").strip()
            conf = float(parsed.get("confidence", 0.85) or 0.85)
        except (KeyError, IndexError, TypeError, ValueError):
            return None
        matched = next((o for o, k in zip(opt_list, keys) if k == target), "")
        if not matched:
            return None
        elapsed = round((time.time() - started) * 1000.0, 1)
        return {
            "choice": matched,
            "confidence": max(0.0, min(1.0, conf)),
            "probs": {matched: max(0.0, min(1.0, conf))},
            "elapsed_ms": elapsed,
            "model": str(body.get("model") or self.model_name),
            "client_ms": elapsed,
        }

    def decide(self, text: str, question: str, options: Sequence[str], *,
               timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """One text, one question, one option set. Never batches."""
        if not text.strip() or not question.strip() or len(options) < 2:
            return None
        if self.decider_backend == "openai":
            return self._decide_openai(text, question, options, timeout=timeout)
        started = time.time()
        body = self._post("/decide",
                          {"text": text, "question": question, "options": list(options)},
                          timeout=timeout)
        if body is None:
            return None
        try:
            confidence = float(body.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        probs_raw = body.get("probs") if isinstance(body.get("probs"), dict) else {}
        probs: Dict[str, float] = {}
        for key, value in probs_raw.items():
            try:
                probs[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        return {
            "choice": str(body.get("choice", "") or ""),
            "confidence": confidence,
            "probs": probs,
            "elapsed_ms": float(body.get("elapsed_ms", 0.0) or 0.0),
            "model": str(body.get("model", "") or ""),
            "client_ms": round((time.time() - started) * 1000.0, 1),
        }

    def embed(self, texts: Sequence[str], *,
              timeout: Optional[float] = None) -> Optional[List[List[float]]]:
        items = [t for t in texts if isinstance(t, str) and t.strip()]
        if not items:
            return None
        body = self._post("/encode", {"texts": items, "return_dense": True,
                                      "return_sparse": False, "return_colbert": False},
                          timeout=timeout)
        if body is None:
            return None
        dense = body.get("dense")
        if not isinstance(dense, list) or len(dense) != len(items):
            return None
        try:
            return [[float(x) for x in row] for row in dense]
        except (TypeError, ValueError):
            return None
