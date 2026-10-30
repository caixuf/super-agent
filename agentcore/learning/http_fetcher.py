#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""有界 HTTP 抓取器：:class:`agentcore.learning.retrieval.Fetcher` 的默认实现。

本模块只做一件事：**一次**受策略约束的 HTTP GET，把响应变成
:class:`~agentcore.learning.retrieval.FetchedSource`。证据校验与落盘仍由
``ingest_fetched`` 负责，本模块不碰候选库、不写正式记忆。

硬边界（全部在发出请求或读取正文的过程中执行）：

- **只允许 http(s) 绝对 URL**；URL 中不得内嵌用户名/密码，且必须是 ASCII
  （不做 IDNA，避免同一主机出现两种编码）；
- **主机白名单**：``allowed_hosts`` 中的条目精确匹配，或作为子域后缀匹配
  （``example.test`` 允许 ``api.example.test``，但不允许
  ``badexample.test``）；IP 字面量只做精确匹配；端口不参与白名单，但每一跳
  重定向都会用同一套规则重新校验；
- **重定向有界**：最多 ``max_redirects`` 跳，拒绝 https → http 降级，拒绝
  带凭证的重定向目标；返回的 ``FetchedSource.url`` 始终是**请求的 URL**，
  不是重定向后的最终 URL（``fetch_and_ingest`` 要求两者一致）；
- **超时**：``timeout`` 是单次 socket 操作（连接/读取）的上限；
- **只接受 2xx**：3xx/4xx/5xx 一律拒收；
- **只接受文本**：``Content-Type`` 必须是 ``text/*`` 或 ``application/json``，
  缺失/空白也拒收；
- **正文有界**：先看 ``Content-Length``，再在读取时卡 ``max_bytes + 1`` 字节，
  超过即 :class:`OversizedSource`（不信任声明值）；
- **不重试**：一次失败就是失败，重试/退避属于调用方；
- **不压缩**：发送 ``Accept-Encoding: identity``，遇到其它
  ``Content-Encoding`` 直接拒收，避免把压缩字节当成证据；
- **不执行 JS、不解析 HTML、不遵守 robots.txt、不维护 Cookie、不做认证、
  不持久化任何状态**。HTTPS 使用标准库默认的证书校验（系统 CA），本模块不
  提供关闭校验的开关。

拒绝分类（复用 ``retrieval`` 的异常层次，调用方捕获 ``CandidateError`` 即可）：

- :class:`FetchRejected`：策略/协议拒绝（scheme、凭证、白名单、状态码、
  Content-Type、Content-Encoding、空正文、重定向超限/降级）；
- :class:`OversizedSource`：正文超过 ``max_bytes``；
- :class:`RetrievalError`：传输层失败（超时、连接失败、解码失败、读取中断）。

注意 ``CandidateError`` 继承自 ``ValueError``，因此本模块内部捕获 ``ValueError``
时必须先放行 ``RetrievalError``，否则策略拒绝会被误包装成「无法发起请求」。

明确不做：robots.txt 解析、限速/重试、真实 TLS 客户端证书、代理配置、
DNS pinning、页面渲染。
"""

from __future__ import annotations

import codecs
import http.client
import ipaddress
import math
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Iterable, Optional, Tuple

from .retrieval import (
    DEFAULT_MAX_BYTES,
    FetchRejected,
    FetchedSource,
    OversizedSource,
    RetrievalError,
)

__all__ = [
    "DEFAULT_MAX_REDIRECTS",
    "DEFAULT_TIMEOUT",
    "DEFAULT_USER_AGENT",
    "HttpFetcher",
]

DEFAULT_TIMEOUT = 5.0
DEFAULT_MAX_REDIRECTS = 2
DEFAULT_USER_AGENT = "agentcore-http-fetcher/1.0"

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_FORBIDDEN_HOST_CHARS = frozenset("/@\\")


# ── 纯函数 ──────────────────────────────────────────────────────────────────
def _is_ip_literal(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _host_is_allowed(host: str, allowed_hosts: Iterable[str]) -> bool:
    """精确匹配，或（非 IP 主机）作为白名单条目的子域匹配。"""
    candidate = host.strip().lower().rstrip(".")
    if not candidate:
        return False
    entries = frozenset(allowed_hosts)
    if candidate in entries:
        return True
    if _is_ip_literal(candidate):
        # ``1.2.3.4`` 不能被当成 ``2.3.4`` 的子域。
        return False
    for entry in entries:
        if _is_ip_literal(entry):
            continue
        if candidate.endswith("." + entry):
            return True
    return False


def _normalize_allowed_hosts(allowed_hosts) -> frozenset:
    if isinstance(allowed_hosts, str):
        raise ValueError("allowed_hosts 必须是主机名集合，而不是单个字符串")
    try:
        entries = list(allowed_hosts)
    except TypeError as exc:
        raise ValueError("allowed_hosts 必须是可迭代的主机名集合") from exc
    if not entries:
        raise ValueError("allowed_hosts 不能为空：默认拒绝一切主机")
    normalized = set()
    for value in entries:
        if not isinstance(value, str):
            raise ValueError("allowed_hosts 条目必须是字符串：%r" % (value,))
        host = value.strip().lower()
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        host = host.rstrip(".")
        if not host:
            raise ValueError("allowed_hosts 不允许空条目")
        if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F
               for ch in host):
            raise ValueError("allowed_hosts 条目不得包含空白/控制字符：%r" % (value,))
        if any(ch in _FORBIDDEN_HOST_CHARS for ch in host):
            raise ValueError("allowed_hosts 条目必须是裸主机名：%r" % (value,))
        if ":" in host and not _is_ip_literal(host):
            raise ValueError("allowed_hosts 条目不得携带端口：%r" % (value,))
        normalized.add(host)
    return frozenset(normalized)


def _positive_finite(value, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s 必须是数字：%r" % (field, value))
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("%s 必须是有限正数：%r" % (field, value))
    return number


def _parse_http_url(url: str) -> Tuple[str, str]:
    """校验并拆分请求 URL，返回 ``(规范化后的原始 URL, 小写主机)``。"""
    if not isinstance(url, str):
        raise FetchRejected("URL 必须是字符串：%r" % (type(url).__name__,))
    raw = url.strip()
    if not raw:
        raise FetchRejected("URL 不能为空")
    if not raw.isascii():
        raise FetchRejected("URL 必须是 ASCII（不支持 IDNA）：%r" % (raw,))
    for ch in raw:
        if ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise FetchRejected("URL 不得包含空白/控制字符")
    if "\\" in raw:
        raise FetchRejected("URL 不得包含反斜杠")
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError as exc:
        raise FetchRejected("URL 无法解析：%r" % (raw,)) from exc
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise FetchRejected("只允许 http(s) URL：%r" % (scheme or raw,))
    if not parts.netloc:
        raise FetchRejected("URL 缺少主机部分：%r" % (raw,))
    if parts.username is not None or parts.password is not None:
        raise FetchRejected("URL 不得内嵌用户名/密码")
    try:
        host = parts.hostname
        parts.port  # 触发端口范围/数字校验
    except ValueError as exc:
        raise FetchRejected("URL 端口非法：%r" % (raw,)) from exc
    if not host:
        raise FetchRejected("URL 缺少主机部分：%r" % (raw,))
    host = host.strip().lower().rstrip(".")
    if not host or any(ch.isspace() or ord(ch) < 0x20 for ch in host):
        raise FetchRejected("URL 主机非法：%r" % (raw,))
    return raw, host


def _is_text_media(media_type: str) -> bool:
    """只接受 ``text/*`` 或 ``application/json``（不含其它 +json 后缀）。"""
    return media_type.startswith("text/") or media_type == "application/json"


def _decode_body(body: bytes, charset: Optional[str]) -> str:
    if charset:
        try:
            codecs.lookup(charset)
        except LookupError as exc:
            raise RetrievalError("未知字符集：%r" % (charset,)) from exc
        try:
            return body.decode(charset, errors="strict")
        except UnicodeDecodeError as exc:
            raise RetrievalError(
                "正文无法按声明的字符集 %r 解码" % (charset,)) from exc
    try:
        # utf-8-sig 兼容 BOM；不猜测 latin-1，避免把损坏的字节当成证据。
        return body.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise RetrievalError("正文不是合法 UTF-8，且响应未声明字符集") from exc


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


class _BoundedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """逐跳校验白名单/降级/次数的重定向处理器（每跳共享一个计数器）。"""

    def __init__(self, *, allowed_hosts, max_redirects: int):
        super().__init__()
        self._allowed_hosts = allowed_hosts
        self._max_redirects = max_redirects
        self._hops = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self._hops += 1
        if self._hops > self._max_redirects:
            raise FetchRejected(
                "重定向超过上限 %d 跳（拒绝继续跟随）" % (self._max_redirects,))
        _, host = _parse_http_url(newurl)
        if not _host_is_allowed(host, self._allowed_hosts):
            raise FetchRejected(
                "重定向目标主机 %r 不在抓取白名单内" % (host,))
        before = urllib.parse.urlsplit(req.full_url).scheme.lower()
        after = urllib.parse.urlsplit(newurl).scheme.lower()
        if before == "https" and after == "http":
            raise FetchRejected("拒绝 https -> http 重定向降级")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpFetcher:
    """有界 HTTP GET 抓取器（实现 ``Fetcher`` 协议）。

    ``fetch`` 只发一次请求，不重试；构造参数校验失败抛 ``ValueError``
    （调用方配置错误），抓取过程中的拒绝抛 ``retrieval`` 层的异常。
    实例不保存请求状态，可安全并发复用。
    """

    def __init__(self, *, allowed_hosts, timeout: float = DEFAULT_TIMEOUT,
                 max_bytes: int = DEFAULT_MAX_BYTES,
                 max_redirects: int = DEFAULT_MAX_REDIRECTS,
                 user_agent: str = DEFAULT_USER_AGENT):
        self.allowed_hosts = _normalize_allowed_hosts(allowed_hosts)
        self.timeout = _positive_finite(timeout, "timeout")
        if (isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
                or max_bytes <= 0):
            raise ValueError("max_bytes 必须是正整数：%r" % (max_bytes,))
        if (isinstance(max_redirects, bool)
                or not isinstance(max_redirects, int)
                or max_redirects < 0):
            raise ValueError("max_redirects 必须是非负整数：%r" % (max_redirects,))
        if not isinstance(user_agent, str) or not user_agent.strip():
            raise ValueError("user_agent 必须是非空字符串")
        if any(ch in user_agent for ch in "\r\n\x00"):
            raise ValueError("user_agent 不得包含换行/NUL")
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.user_agent = user_agent.strip()

    def __repr__(self) -> str:
        return (
            "HttpFetcher(allowed_hosts=%r, timeout=%r, max_bytes=%r, "
            "max_redirects=%r)" % (
                tuple(sorted(self.allowed_hosts)), self.timeout,
                self.max_bytes, self.max_redirects))

    # ── 主入口 ──
    def fetch(self, url: str) -> FetchedSource:
        raw, host = _parse_http_url(url)
        if not _host_is_allowed(host, self.allowed_hosts):
            raise FetchRejected("主机 %r 不在抓取白名单内" % (host,))

        request = urllib.request.Request(
            raw,
            method="GET",
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/*, application/json;q=0.9",
                # 不自动解压：压缩字节不能当证据。
                "Accept-Encoding": "identity",
                "Connection": "close",
            },
        )
        opener = urllib.request.build_opener(_BoundedRedirectHandler(
            allowed_hosts=self.allowed_hosts,
            max_redirects=self.max_redirects,
        ))
        try:
            response = opener.open(request, timeout=self.timeout)
        except RetrievalError:
            # CandidateError 本身是 ValueError，必须先放行策略/检索异常。
            raise
        except urllib.error.HTTPError as exc:
            status = getattr(exc, "code", "?")
            try:
                exc.close()
            except Exception:  # pragma: no cover - best effort
                pass
            raise FetchRejected(
                "HTTP 状态 %s 不是成功响应：%s" % (status, raw)) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise RetrievalError(
                "抓取超时（%.3gs）：%s" % (self.timeout, raw)) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise RetrievalError(
                    "抓取超时（%.3gs）：%s" % (self.timeout, raw)) from exc
            raise RetrievalError("抓取失败：%s（%s）" % (raw, reason)) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise RetrievalError("抓取失败：%s（%s）" % (raw, exc)) from exc
        except ValueError as exc:
            # urllib 对无法编码/不支持的 URL 抛 ValueError。
            raise FetchRejected("无法发起请求：%r" % (raw,)) from exc

        with response:
            status = getattr(response, "status", None)
            if status is None:  # pragma: no cover - 兼容旧版 HTTPResponse
                status = response.getcode()
            if not isinstance(status, int) or not (200 <= status < 300):
                raise FetchRejected(
                    "HTTP 状态 %r 不是成功响应：%s" % (status, raw))

            content_type = response.headers.get("Content-Type")
            if content_type is None:
                raise FetchRejected("响应缺少 Content-Type，拒绝作为证据")
            media_type = content_type.split(";", 1)[0].strip().lower()
            if not _is_text_media(media_type):
                raise FetchRejected(
                    "content_type %r 不是 text/* 或 application/json，拒绝作为证据"
                    % (content_type,))

            content_encoding = (response.headers.get("Content-Encoding")
                                or "identity").strip().lower()
            if content_encoding not in ("", "identity"):
                raise FetchRejected(
                    "不支持的 Content-Encoding %r（不做解压，避免压缩字节入库）"
                    % (content_encoding,))

            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    declared_length = int(declared.strip())
                except ValueError:
                    declared_length = None
                if declared_length is not None and declared_length > self.max_bytes:
                    raise OversizedSource(
                        "声明的正文 %d 字节超过上限 %d 字节：%s"
                        % (declared_length, self.max_bytes, raw))

            try:
                body = response.read(self.max_bytes + 1)
            except (socket.timeout, TimeoutError) as exc:
                raise RetrievalError("读取响应超时：%s" % (raw,)) from exc
            except (OSError, http.client.HTTPException) as exc:
                raise RetrievalError("读取响应失败：%s（%s）" % (raw, exc)) from exc

            charset = response.headers.get_content_charset()

        if len(body) > self.max_bytes:
            raise OversizedSource(
                "正文超过上限 %d 字节：%s" % (self.max_bytes, raw))
        text = _decode_body(body, charset)
        if not text.strip():
            raise FetchRejected("响应正文为空，拒绝作为证据：%s" % (raw,))

        return FetchedSource.from_text(
            url=raw,
            text=text,
            retrieved_at=_utcnow(),
            http_status=status,
            content_type=content_type,
        )
