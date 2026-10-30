"""HttpFetcher 的本地回环验收：白名单、重定向、大小、类型、超时、灌入。

全部请求只打到 ``127.0.0.1`` 上临时启动的 ``http.server``；被策略拒绝的 URL
在建立连接前就被拦下，不产生任何外部网络流量。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agentcore.learning import CandidateStore
from agentcore.learning.http_fetcher import HttpFetcher, _host_is_allowed
from agentcore.learning.retrieval import (
    FetchRejected,
    Fetcher,
    OversizedSource,
    RetrievalError,
    content_hash_of,
    fetch_and_ingest,
)

TEXT = (
    "Widget 4.2 release notes\n"
    "Widget 4.2 was released on 2026-03-04.\n"
)
QUOTE = "Widget 4.2 was released on 2026-03-04."
JSON_BODY = '{"product": "widget", "version": "4.2"}'
DOMAIN = "release-notes"
BIG_BYTES = 4096


# ── 本地回环服务器 ──────────────────────────────────────────────────────────
def _send(handler, *, body: bytes = b"", status: int = 200,
          content_type="text/plain; charset=utf-8", headers=None,
          delay: float = 0.0):
    if delay:
        time.sleep(delay)
    handler.send_response(status)
    if content_type is not None:
        handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    handler.end_headers()
    if body:
        handler.wfile.write(body)
    handler.wfile.flush()


def _declared_oversized(handler):
    """声明一个很大的 Content-Length，正文却很小：必须在读取前拒收。"""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/plain")
    handler.send_header("Content-Length", str(BIG_BYTES))
    handler.end_headers()
    handler.wfile.write(b"tiny")
    handler.wfile.flush()


def _slow_body(handler):
    """先回响应头，再拖住正文：覆盖读超时（而不是连接超时）路径。"""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/plain")
    handler.send_header("Content-Length", "4")
    handler.end_headers()
    handler.wfile.flush()
    time.sleep(1.0)
    handler.wfile.write(b"late")
    handler.wfile.flush()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        route = self.server.routes.get(self.path)
        if route is None:
            _send(self, status=404, body=b"not found")
            return
        try:
            route(self)
        except (BrokenPipeError, ConnectionResetError):
            # 客户端超时/拒收后主动断开，属于预期路径。
            pass

    def log_message(self, *args):  # 保持测试输出干净
        pass


@pytest.fixture(scope="module")
def server():
    routes = {
        "/ok": lambda h: _send(h, body=TEXT.encode("utf-8")),
        "/json": lambda h: _send(h, body=JSON_BODY.encode("utf-8"),
                                 content_type="application/json"),
        "/empty": lambda h: _send(h, body=b""),
        "/missing": lambda h: _send(h, status=404, body=b"not found"),
        "/big": lambda h: _send(h, body=b"x" * BIG_BYTES),
        "/declared-big": _declared_oversized,
        "/image": lambda h: _send(h, body=b"\x89PNG\r\n",
                                  content_type="image/png"),
        "/no-type": lambda h: _send(h, body=b"hello", content_type=None),
        "/gzip": lambda h: _send(h, body=b"\x1f\x8b\x08",
                                 headers={"Content-Encoding": "gzip"}),
        "/redirect-ok": lambda h: _send(
            h, status=302, headers={"Location": "/ok"}),
        "/redirect-evil": lambda h: _send(
            h, status=302, headers={"Location": "http://evil.invalid/ok"}),
        "/redirect-loop": lambda h: _send(
            h, status=302, headers={"Location": "/redirect-loop"}),
        "/slow": lambda h: _send(h, body=b"late", delay=1.0),
        "/slow-body": _slow_body,
    }
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.routes = routes
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        yield "http://%s:%d" % (host, port)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5.0)


def _fetcher(**overrides) -> HttpFetcher:
    kwargs = dict(allowed_hosts={"127.0.0.1"}, timeout=2.0)
    kwargs.update(overrides)
    return HttpFetcher(**kwargs)


# ── 1. 正常路径 ─────────────────────────────────────────────────────────────
def test_success_returns_fetch_result_and_feeds_quoted_ingest(tmp_path, server):
    fetcher = _fetcher()
    url = server + "/ok"

    assert isinstance(fetcher, Fetcher)
    fetched = fetcher.fetch(url)

    assert fetched.url == url  # 重定向后也保持请求 URL 的身份
    assert fetched.text == TEXT
    assert fetched.http_status == 200
    assert fetched.content_type.startswith("text/plain")
    assert fetched.content_hash == content_hash_of(TEXT)
    assert fetched.hash_matches() is True
    assert fetched.retrieved_at.endswith("Z")
    parsed = datetime.fromisoformat(fetched.retrieved_at.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None

    store = CandidateStore(tmp_path / "candidate-pool")
    result = fetch_and_ingest(
        store, fetcher, url,
        statement=QUOTE, literal_quote=QUOTE, domain=DOMAIN,
        subject="widget-4.2", author="retriever",
    )
    assert result.created is True
    assert result.candidate.review_status == "pending"
    assert result.candidate.source_hash == content_hash_of(TEXT)
    assert store.snapshot_text(result.candidate.source_id) == TEXT


def test_application_json_is_accepted(server):
    fetched = _fetcher().fetch(server + "/json")

    assert fetched.text == JSON_BODY
    assert fetched.content_type == "application/json"
    assert fetched.content_hash == content_hash_of(JSON_BODY)


def test_redirect_to_allowed_host_is_followed(server):
    url = server + "/redirect-ok"

    fetched = _fetcher().fetch(url)

    assert fetched.url == url
    assert fetched.text == TEXT
    assert fetched.http_status == 200


# ── 2. 状态 / 内容类型 / 大小 ────────────────────────────────────────────────
def test_404_is_rejected(server):
    with pytest.raises(FetchRejected) as excinfo:
        _fetcher().fetch(server + "/missing")

    assert "404" in str(excinfo.value)


@pytest.mark.parametrize("path", ["/image", "/no-type"])
def test_non_text_content_is_rejected(server, path):
    with pytest.raises(FetchRejected):
        _fetcher().fetch(server + path)


def test_empty_body_is_rejected(server):
    with pytest.raises(FetchRejected):
        _fetcher().fetch(server + "/empty")


def test_compressed_body_is_rejected(server):
    with pytest.raises(FetchRejected) as excinfo:
        _fetcher().fetch(server + "/gzip")

    assert "Content-Encoding" in str(excinfo.value)


def test_oversized_body_is_rejected(server):
    with pytest.raises(OversizedSource) as excinfo:
        _fetcher(max_bytes=1024).fetch(server + "/big")

    assert not isinstance(excinfo.value, FetchRejected)


def test_declared_oversized_length_is_rejected_before_reading(server):
    with pytest.raises(OversizedSource):
        _fetcher(max_bytes=1024).fetch(server + "/declared-big")


# ── 3. 重定向策略 ───────────────────────────────────────────────────────────
def test_redirect_to_disallowed_host_is_rejected(server):
    with pytest.raises(FetchRejected) as excinfo:
        _fetcher().fetch(server + "/redirect-evil")

    assert "白名单" in str(excinfo.value)


def test_redirect_limit_is_enforced(server):
    with pytest.raises(FetchRejected) as excinfo:
        _fetcher(max_redirects=1).fetch(server + "/redirect-loop")

    assert "重定向超过上限" in str(excinfo.value)


def test_zero_redirects_refuses_any_redirect(server):
    with pytest.raises(FetchRejected):
        _fetcher(max_redirects=0).fetch(server + "/redirect-ok")


# ── 4. 超时与传输层 ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("path", ["/slow", "/slow-body"])
def test_timeout_raises_retrieval_error_not_policy_rejection(server, path):
    with pytest.raises(RetrievalError) as excinfo:
        _fetcher(timeout=0.2).fetch(server + path)

    assert not isinstance(excinfo.value, FetchRejected)


# ── 5. 白名单与 URL 策略 ────────────────────────────────────────────────────
def test_host_not_in_allowlist_is_rejected_before_network(server):
    with pytest.raises(FetchRejected) as excinfo:
        _fetcher(allowed_hosts={"allowed.invalid"}).fetch(server + "/ok")

    assert "白名单" in str(excinfo.value)


def test_credentials_in_url_are_rejected(server):
    url = server.replace("http://", "http://user:secret@") + "/ok"

    with pytest.raises(FetchRejected) as excinfo:
        _fetcher().fetch(url)

    assert "用户名/密码" in str(excinfo.value)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://127.0.0.1/pub",
    "//127.0.0.1/ok",
    "http:///ok",
    "http://127.0.0.1:not-a-port/ok",
])
def test_non_http_or_malformed_urls_are_rejected(url):
    with pytest.raises(FetchRejected):
        _fetcher().fetch(url)


def test_allowlist_matches_exact_hosts_and_subdomains_only():
    allowed = {"example.test", "127.0.0.1", "::1"}

    assert _host_is_allowed("example.test", allowed)
    assert _host_is_allowed("API.Example.Test.", allowed)
    assert _host_is_allowed("api.example.test", allowed)
    assert not _host_is_allowed("badexample.test", allowed)
    assert not _host_is_allowed("example.test.evil.invalid", allowed)
    assert _host_is_allowed("127.0.0.1", allowed)
    assert not _host_is_allowed("1.2.3.4", {"2.3.4"})
    assert _host_is_allowed("::1", allowed)


# ── 6. 构造参数校验 ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("kwargs", [
    {"allowed_hosts": set()},
    {"allowed_hosts": "example.test"},
    {"allowed_hosts": {"example.test"}, "timeout": 0},
    {"allowed_hosts": {"example.test"}, "timeout": float("inf")},
    {"allowed_hosts": {"example.test"}, "max_bytes": 0},
    {"allowed_hosts": {"example.test"}, "max_bytes": True},
    {"allowed_hosts": {"example.test"}, "max_redirects": -1},
    {"allowed_hosts": {"example.test"}, "user_agent": ""},
    {"allowed_hosts": {"example.test"}, "user_agent": "bad\nagent"},
    {"allowed_hosts": {"exa mple.test"}},
    {"allowed_hosts": {"example.test:8080"}},
    {"allowed_hosts": {"http://example.test"}},
])
def test_constructor_rejects_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        HttpFetcher(**kwargs)


def test_repr_does_not_expose_request_state():
    repr_text = repr(_fetcher(allowed_hosts={"example.test", "api.example.test"}))

    assert "api.example.test" in repr_text
    assert "timeout=2.0" in repr_text
