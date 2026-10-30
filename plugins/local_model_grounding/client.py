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
                 timeout: float = 10.0) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.token = read_token(token, token_file)
        try:
            self.timeout = float(timeout)
        except (TypeError, ValueError):
            self.timeout = 10.0
        if self.timeout <= 0:
            self.timeout = 10.0

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _post(self, path: str, payload: Dict[str, Any],
              timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        if not self.base_url:
            return None
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.base_url + path, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with _OPENER.open(request, timeout=timeout or self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
            return None
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def decide(self, text: str, question: str, options: Sequence[str], *,
               timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """One text, one question, one option set. Never batches."""
        if not text.strip() or not question.strip() or len(options) < 2:
            return None
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
