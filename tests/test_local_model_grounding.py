"""Hermes plugin tests for local-model-grounding (fake gateway, no network)."""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO / "plugins/local_model_grounding"

_spec = importlib.util.spec_from_file_location(
    "local_model_grounding", PLUGIN_DIR / "__init__.py",
    submodule_search_locations=[str(PLUGIN_DIR)])
PLUGIN = importlib.util.module_from_spec(_spec)
sys.modules["local_model_grounding"] = PLUGIN
_spec.loader.exec_module(PLUGIN)


class Context:
    def __init__(self, **settings):
        self.settings = settings
        self.hooks = {}
        self.profile_name = settings.get("profile_name", "test-profile")

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_hook(self, name, callback):
        self.hooks[name] = callback


class _Handler(BaseHTTPRequestHandler):
    responses = {}
    requests = []

    def do_POST(self):  # noqa: N802 - stdlib
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        type(self).requests.append({
            "path": self.path,
            "body": json.loads(raw) if raw else {},
            "auth": self.headers.get("Authorization"),
        })
        spec = type(self).responses.get(self.path)
        if spec is None:
            self.send_response(404)
            self.end_headers()
            return
        status, payload, delay = spec
        if delay:
            time.sleep(delay)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        return


class Gateway:
    def __init__(self):
        self.handler = type("_H", (_Handler,), {"responses": {}, "requests": []})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def requests(self):
        return self.handler.requests

    def reply(self, path, payload, *, status=200, delay=0.0):
        self.handler.responses[path] = (status, payload, delay)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def _decision(choice, confidence):
    return {"choice": choice, "confidence": confidence, "probs": {choice: confidence},
            "elapsed_ms": 9.0, "model": "decider-4b:test"}


def _context(tmp_path, gateway_url="", **extra):
    settings = {
        "enabled": True,
        "gateway_url": gateway_url,
        "audit_path": str(tmp_path / "audit.jsonl"),
        "min_confidence": 0.6,
        "profile_name": "test-profile",
    }
    settings.update(extra)
    return Context(**settings)


def _history(text):
    return [{"role": "tool", "content": text}]


def test_default_disabled_registers_nothing():
    context = Context()
    PLUGIN.register(context)
    assert context.hooks == {}


def test_enabled_without_gateway_registers_nothing(tmp_path):
    context = _context(tmp_path, gateway_url="")
    PLUGIN.register(context)
    assert context.hooks == {}


def test_turn_without_failure_does_not_call_decider(tmp_path):
    server = Gateway().start()
    try:
        context = _context(tmp_path, gateway_url=server.url)
        PLUGIN.register(context)
        hook = context.hooks["pre_llm_call"]
        assert hook(session_id="s1", user_message="今天状态机怎么跳转？",
                    conversation_history=[{"role": "user", "content": "你好"}]) is None
        assert server.requests == []
    finally:
        server.stop()


def test_decisive_environment_verdict_injects_stop_editing(tmp_path):
    server = Gateway().start()
    try:
        server.reply("/decide", _decision("数据缺失", 0.93))
        context = _context(tmp_path, gateway_url=server.url)
        PLUGIN.register(context)
        result = context.hooks["pre_llm_call"](
            session_id="s1", user_message="这个用例为什么挂了",
            conversation_history=_history("ERROR: map tile 32-118 not found"))
        assert result and "数据缺失" in result["context"]
        assert "不要为了变绿去改断言" in result["context"]
        rows = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
        decider = [r for r in rows if r["kind"] == "decider"][0]
        assert decider["decisive"] is True and decider["skip_llm"] is True
        assert decider["source"] == "conversation_history"
        assert decider["options"] == list(PLUGIN.PRESCREEN_OPTIONS)
    finally:
        server.stop()


def test_low_confidence_injects_nothing_but_is_audited(tmp_path):
    server = Gateway().start()
    try:
        server.reply("/decide", _decision("数据缺失", 0.41))
        context = _context(tmp_path, gateway_url=server.url)
        PLUGIN.register(context)
        result = context.hooks["pre_llm_call"](
            session_id="s1", user_message="看看这个失败",
            conversation_history=_history("ERROR: map tile missing"))
        assert result is None
        rows = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
        decider = [r for r in rows if r["kind"] == "decider"][0]
        assert decider["decisive"] is False and decider["skip_llm"] is False
    finally:
        server.stop()


def test_threshold_cannot_be_lowered(tmp_path):
    context = _context(tmp_path, gateway_url="http://127.0.0.1:9", min_confidence=0.1)
    grounding = PLUGIN.Grounding(context)
    assert grounding.min_confidence == PLUGIN.MIN_CONFIDENCE_FLOOR


def test_gateway_error_degrades_silently(tmp_path):
    server = Gateway().start()
    try:
        server.reply("/decide", {"detail": "boom"}, status=500)
        context = _context(tmp_path, gateway_url=server.url)
        PLUGIN.register(context)
        assert context.hooks["pre_llm_call"](
            session_id="s1", user_message="失败了",
            conversation_history=_history("ERROR: boom")) is None
        rows = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
        decider = [r for r in rows if r["kind"] == "decider"][0]
        assert decider["available"] is False
    finally:
        server.stop()


def test_gateway_timeout_degrades_silently(tmp_path):
    server = Gateway().start()
    try:
        server.reply("/decide", _decision("环境问题", 0.99), delay=1.0)
        context = _context(tmp_path, gateway_url=server.url, timeout=0.2)
        PLUGIN.register(context)
        started = time.time()
        result = context.hooks["pre_llm_call"](
            session_id="s1", user_message="失败",
            conversation_history=_history("ERROR: slow"))
        assert result is None
        assert time.time() - started < 1.0
    finally:
        server.stop()


def test_token_file_is_used(tmp_path):
    server = Gateway().start()
    try:
        server.reply("/decide", _decision("断言不匹配", 0.9))
        token_file = tmp_path / "token"
        token_file.write_text("s3cret\n", encoding="utf-8")
        context = _context(tmp_path, gateway_url=server.url, token_file=str(token_file))
        PLUGIN.register(context)
        context.hooks["pre_llm_call"](
            session_id="s1", user_message="失败",
            conversation_history=_history("AssertionError: step 120"))
        assert server.requests[0]["auth"] == "Bearer s3cret"
    finally:
        server.stop()


def _fake_embed(texts, *, timeout=None):
    vectors = []
    for text in texts:
        lowered = text.lower()
        vectors.append([
            1.0 if "磁盘" in text else 0.0,
            1.0 if "地图" in text else 0.0,
            1.0 if "assert" in lowered or "断言" in text else 0.0,
        ])
    return vectors


def test_precedent_block_is_dated_and_labelled(tmp_path, monkeypatch):
    docs = tmp_path / "docs" / "handover"
    docs.mkdir(parents=True)
    (docs / "2026-09-20_disk-full_handover.md").write_text(
        "# 磁盘写满\n\n"
        "容器退出原因是宿主机磁盘写满，清理后恢复。\n"
        "现场证据：/var/lib/docker 占用 98%，删除旧镜像后容器可正常启动。\n"
        "复现条件：连续跑三批基线用例后磁盘增长约 8GB。\n", encoding="utf-8")

    server = Gateway().start()
    try:
        server.reply("/decide", _decision("断言不匹配", 0.9))
        context = _context(tmp_path, gateway_url=server.url,
                           docs_roots=[str(docs)], index_dir=str(tmp_path / "index"))
        grounding = PLUGIN.Grounding(context)
        monkeypatch.setattr(grounding.client, "embed", _fake_embed)
        assert grounding.build_index_now() is not None
        result = grounding.pre_llm_call(
            session_id="s1", user_message="磁盘写满 容器退出",
            conversation_history=[])
        assert result and "先例检索" in result["context"]
        assert "[2026-09-20]" in result["context"]
        assert "非当前真相" in result["context"]
    finally:
        server.stop()


def test_duplicate_injection_is_suppressed(tmp_path):
    server = Gateway().start()
    try:
        server.reply("/decide", _decision("数据缺失", 0.93))
        context = _context(tmp_path, gateway_url=server.url)
        PLUGIN.register(context)
        hook = context.hooks["pre_llm_call"]
        history = _history("ERROR: map tile missing")
        first = hook(session_id="s1", user_message="为什么挂了", conversation_history=history)
        second = hook(session_id="s1", user_message="为什么挂了", conversation_history=history)
        assert first is not None
        assert second is None
        assert len([r for r in server.requests if r["path"] == "/decide"]) == 1
    finally:
        server.stop()


def test_session_caches_stay_bounded(tmp_path):
    """会话级缓存必须有上限 —— 长跑网关会为每个会话留一条，永不回收就等于泄漏。"""
    server = Gateway().start()
    try:
        context = _context(tmp_path, gateway_url=server.url)
        PLUGIN.register(context)
        grounding = PLUGIN.Grounding(context)
        for index in range(PLUGIN._MAX_VERDICT_SESSIONS + 40):
            grounding._verdicts['s%d' % index] = {'digest': 'd', 'source': 'x', 'choice': 'a',
                                                'confidence': 1.0, 'decisive': True, 'skip_llm': True,
                                                'model': 'm', 'elapsed_ms': 0.0}
        grounding._trim_session_keys_locked(grounding._verdicts, PLUGIN._MAX_VERDICT_SESSIONS)
        assert len(grounding._verdicts) == PLUGIN._MAX_VERDICT_SESSIONS
        for index in range(PLUGIN._MAX_INJECTED_SESSIONS + 40):
            grounding._injected['s%d' % index] = {'h'}
        grounding._trim_session_keys_locked(grounding._injected, PLUGIN._MAX_INJECTED_SESSIONS)
        assert len(grounding._injected) == PLUGIN._MAX_INJECTED_SESSIONS
    finally:
        server.stop()
