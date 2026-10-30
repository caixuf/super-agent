#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检索抓取契约：把一次**已经完成**的外部抓取送进候选库。

本模块位于「检索」与「候选知识」之间，只做一件有边界的事::

    已抓取快照（FetchedSource） → 证据校验 → CandidateStore.ingest

边界与候选层保持一致：

- **不联网**：模块不 import socket/http/urllib/requests；抓取动作由调用方以
  :class:`Fetcher` 注入。本模块只接收抓取**结果**，因此测试可以完全离线，
  生产也不会在这里偷偷发请求。
- **不定时**：没有 sleep / Timer / 调度；``retrieved_at`` 是证据字段。
- **不写正式记忆**：只创建 ``pending`` 候选；审批与入库仍是后续阶段的动作。
- **不信任抓取结果**：非 2xx、非文本内容、空正文、超限、哈希不一致、
  引文不逐字命中，全部拒收，并且拒绝发生在落盘之前。

内容哈希一律由本模块从正文重新计算（``sha256(utf-8)``）。``FetchedSource``
里声明的 ``content_hash`` 只是调用方断言，**待核对**：不一致立刻拒绝。这样
「先把声明写对、再把正文换掉」的路径走不通。

本阶段明确不做：真实抓取器实现、重试/限速/robots、审查交互、``write_fact``
正式入库、写入后的召回探针。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional, Protocol, Union, runtime_checkable

from .candidates import (
    CandidateError,
    EvidenceMismatch,
    IngestResult,
    InvalidCandidate,
    QuoteNotGrounded,
    normalize_source_url,
    quote_grounded,
)

__all__ = [
    "DEFAULT_MAX_BYTES",
    "FetchRejected",
    "FetchedSource",
    "Fetcher",
    "OversizedSource",
    "RetrievalError",
    "content_hash_of",
    "fetch_and_ingest",
    "ingest_fetched",
]

#: 单次抓取正文的默认上限（字节）。候选层自身还有 2 MiB 的硬上限，
#: 这里取更小的一档：证据应当足以支撑一句引文，而不是搬运整站。
DEFAULT_MAX_BYTES = 256 * 1024

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

_TEXTUAL_APPLICATION_TYPES = frozenset({
    "application/json",
    "application/ld+json",
    "application/xml",
    "application/xhtml+xml",
    "application/javascript",
    "application/x-ndjson",
})


# ── 异常 ────────────────────────────────────────────────────────────────────
class RetrievalError(CandidateError):
    """检索/抓取契约层的拒绝基类；调用方捕获 ``CandidateError`` 即可。"""


class FetchRejected(RetrievalError):
    """抓取结果本身不可用：非成功状态、非文本内容、空正文、来源不匹配。"""


class OversizedSource(RetrievalError):
    """正文超过 ``max_bytes``，拒绝进入候选库。"""


# ── 纯函数 ──────────────────────────────────────────────────────────────────
def content_hash_of(data: Union[str, bytes, bytearray]) -> str:
    """从正文重新计算 ``sha256``（字节级，UTF-8 编码）。"""
    if isinstance(data, str):
        payload = data.encode("utf-8")
    elif isinstance(data, (bytes, bytearray)):
        payload = bytes(data)
    else:
        raise InvalidCandidate("正文必须是 str 或 bytes：%r" % (type(data).__name__,))
    return hashlib.sha256(payload).hexdigest()


def _require_str(value, field: str, *, allow_blank: bool = False) -> str:
    if not isinstance(value, str):
        raise InvalidCandidate("%s 必须是字符串" % field)
    if "\x00" in value:
        raise InvalidCandidate("%s 不得包含 NUL 字符" % field)
    if not allow_blank and not value.strip():
        raise InvalidCandidate("%s 不能为空" % field)
    return value


def _declared_hash(value) -> str:
    """规范化调用方声明的哈希；只接受 ``sha256:<hex>`` 或裸 hex。"""
    raw = _require_str(value, "content_hash").strip().lower()
    if raw.startswith("sha256:"):
        raw = raw[len("sha256:"):]
    if not _HASH_RE.match(raw):
        raise InvalidCandidate(
            "content_hash 必须是 64 位十六进制 sha256：%r" % (value,))
    return raw


def _is_textual_content_type(value: str) -> bool:
    media = value.split(";", 1)[0].strip().lower()
    if not media:
        return False
    if media.startswith("text/"):
        return True
    if media in _TEXTUAL_APPLICATION_TYPES:
        return True
    return media.endswith("+json") or media.endswith("+xml")


# ── 抓取结果与抓取器契约 ────────────────────────────────────────────────────
@dataclass(frozen=True)
class FetchedSource:
    """一次抓取的不可变结果。

    ``content_hash`` 是调用方**声明**的 ``sha256(正文)``；真正入库前会重新
    计算并逐字比对。``http_status`` / ``content_type`` 可选，但一旦提供就会
    参与门禁（非 2xx、非文本内容直接拒收）。
    """

    url: str
    text: str
    content_hash: str
    retrieved_at: str
    http_status: Optional[int] = None
    content_type: Optional[str] = None

    def __post_init__(self) -> None:
        _require_str(self.url, "url")
        _require_str(self.text, "text", allow_blank=True)
        _require_str(self.content_hash, "content_hash")
        _require_str(self.retrieved_at, "retrieved_at")
        if self.http_status is not None:
            if isinstance(self.http_status, bool) or not isinstance(self.http_status, int):
                raise InvalidCandidate("http_status 必须是整数状态码")
        if self.content_type is not None:
            # 空/空白 content_type 不在构造期判死，而是留给抓取门禁按
            # 「非文本内容」统一拒收，错误类型保持一致。
            _require_str(self.content_type, "content_type", allow_blank=True)

    @classmethod
    def from_text(cls, *, url: str, text: str, retrieved_at: str,
                  http_status: Optional[int] = None,
                  content_type: Optional[str] = None) -> "FetchedSource":
        """便捷构造：哈希由正文算好，调用方无需手写。"""
        return cls(url=url, text=text, content_hash=content_hash_of(text),
                   retrieved_at=retrieved_at, http_status=http_status,
                   content_type=content_type)

    @property
    def computed_hash(self) -> str:
        """按正文重新计算的哈希（不信任声明值）。"""
        return content_hash_of(self.text)

    @property
    def byte_size(self) -> int:
        return len(self.text.encode("utf-8"))

    def hash_matches(self) -> bool:
        try:
            return _declared_hash(self.content_hash) == self.computed_hash
        except InvalidCandidate:
            return False


@runtime_checkable
class Fetcher(Protocol):
    """抓取器协议：由调用方注入。本模块自身不实现、也不调用网络。

    实现方负责网络与重试；返回的 ``FetchedSource.url`` 必须与请求 URL 一致，
    否则 :func:`fetch_and_ingest` 视为来源不匹配并拒收。
    """

    def fetch(self, url: str) -> FetchedSource:
        ...


# ── 主流程 ──────────────────────────────────────────────────────────────────
def ingest_fetched(store, fetched, *, statement, literal_quote, domain,
                   subject=None, author=None,
                   max_bytes: int = DEFAULT_MAX_BYTES) -> IngestResult:
    """校验一份抓取结果并写入**候选库**（只能是 ``pending``）。

    校验顺序（任何一步失败都不会落盘）：

    1. 类型与 ``max_bytes`` 合法；
    2. 正文非空且不超过 ``max_bytes``；
    3. ``http_status``（若给出）为 2xx；
    4. ``content_type``（若给出）为文本类型；
    5. 从正文重算哈希，与声明值一致；
    6. 引文在正文中**逐字**出现（不做归一化）；
    7. 交给 :meth:`CandidateStore.ingest` 落盘（它会再次复核 5、6）。

    返回 ``IngestResult``：重复的「来源 + 候选」会带 ``created=False``，
    同一来源的不同引文共享一份快照但各自成为独立候选。
    """
    if not isinstance(fetched, FetchedSource):
        raise InvalidCandidate("fetched 必须是 FetchedSource")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise InvalidCandidate("max_bytes 必须是正整数：%r" % (max_bytes,))

    text = fetched.text
    payload = text.encode("utf-8")
    if not text.strip():
        raise FetchRejected("抓取正文为空：没有原文就没有逐字证据")
    if len(payload) > max_bytes:
        raise OversizedSource(
            "抓取正文 %d 字节超过上限 %d 字节" % (len(payload), max_bytes))

    if fetched.http_status is not None and not (200 <= fetched.http_status < 300):
        raise FetchRejected(
            "HTTP 状态 %d 不是成功响应" % (fetched.http_status,))
    if fetched.content_type is not None and not _is_textual_content_type(
            fetched.content_type):
        raise FetchRejected(
            "content_type %r 不是文本内容，拒绝作为证据" % (fetched.content_type,))

    computed = content_hash_of(payload)
    declared = _declared_hash(fetched.content_hash)
    if declared != computed:
        raise EvidenceMismatch(
            "content_hash 与正文实际哈希不一致：声明 %s，实际 %s"
            % (declared, computed))

    if not quote_grounded(text, literal_quote):
        raise QuoteNotGrounded(
            "引文无法在抓取正文中逐字定位（不做大小写/空白归一化）；拒绝写入候选")

    return store.ingest(
        statement=statement,
        literal_quote=literal_quote,
        source_url=fetched.url,
        retrieved_at=fetched.retrieved_at,
        domain=domain,
        snapshot=text,
        source_hash=computed,
        subject=subject,
        author=author,
    )


def fetch_and_ingest(store, fetcher, url: str, *, statement, literal_quote,
                     domain, subject=None, author=None,
                     max_bytes: int = DEFAULT_MAX_BYTES) -> IngestResult:
    """注入式抓取 + 灌入：``fetcher`` 是唯一的外部 I/O 入口。

    本函数只做协议核对（实现存在、返回类型正确、来源未被静默替换），
    校验与落盘全部复用 :func:`ingest_fetched`。测试里传入假抓取器即可
    验证整条链路，不会产生任何网络请求。
    """
    if fetcher is None or not callable(getattr(fetcher, "fetch", None)):
        raise InvalidCandidate("fetcher 必须实现 fetch(url) -> FetchedSource")
    requested = normalize_source_url(url)
    fetched = fetcher.fetch(requested)
    if not isinstance(fetched, FetchedSource):
        raise FetchRejected("fetcher 未返回 FetchedSource：%r" % (type(fetched).__name__,))
    returned = normalize_source_url(fetched.url)
    if returned != requested:
        raise FetchRejected(
            "fetcher 返回的来源与请求不匹配：请求 %s，返回 %s"
            % (requested, returned))
    return ingest_fetched(
        store,
        fetched,
        statement=statement,
        literal_quote=literal_quote,
        domain=domain,
        subject=subject,
        author=author,
        max_bytes=max_bytes,
    )
