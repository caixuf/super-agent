"""Dated precedent retrieval over handover/evidence markdown.

Chunks are embedded once by bge-m3 and stored as a flat float32 file plus JSON
metadata, so the plugin works on a host without numpy. A query is one embedding
call plus a linear scan; at ~3000 chunks that is tens of milliseconds.

Everything here is optional infrastructure: missing index, missing gateway or a
malformed cache all degrade to "no precedents", never to an exception.
"""
from __future__ import annotations

import array
import hashlib
import json
import operator
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

INDEX_VERSION = 1
DEFAULT_TOP_K = 5
DEFAULT_MIN_SCORE = 0.35
MAX_BLOCK_CHARS = 2200
MAX_INDEX_CHUNKS = 4000
_CHUNK_CHARS = 1000
_MIN_CHUNK_CHARS = 80
_MAX_CHUNKS_PER_FILE = 2

_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _date_of(name: str) -> str:
    match = _DATE_RE.search(name)
    if not match:
        return ""
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"


def _flush(path: str, heading: str, date: str, buf: List[str]) -> Iterable[Dict[str, str]]:
    text = "\n".join(buf).strip()
    if len(text) < _MIN_CHUNK_CHARS:
        return []
    return [{"path": path, "heading": heading, "date": date, "text": text}]


def _iter_chunks(roots: Sequence[Path]) -> Iterable[Dict[str, str]]:
    for root in roots:
        for path in sorted(root.rglob("*.md")):
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            rel = str(path.relative_to(root)).replace("\\", "/")
            date = _date_of(path.name) or _date_of(rel)
            heading = ""
            buf: List[str] = []
            for line in lines:
                match = _HEADING_RE.match(line)
                if match:
                    yield from _flush(rel, heading, date, buf)
                    heading = match.group(2).strip()
                    buf = []
                    continue
                buf.append(line)
                if sum(len(x) + 1 for x in buf) >= _CHUNK_CHARS:
                    yield from _flush(rel, heading, date, buf)
                    buf = []
            yield from _flush(rel, heading, date, buf)


def _signature(roots: Sequence[Path]) -> Dict[str, float]:
    sig: Dict[str, float] = {}
    for root in roots:
        for path in root.rglob("*.md"):
            try:
                sig[str(path)] = round(path.stat().st_mtime, 3)
            except OSError:
                continue
    return sig


def _normalize(vector: Sequence[float]) -> List[float]:
    norm = sum(float(x) * float(x) for x in vector) ** 0.5
    if norm <= 1e-9:
        return [0.0 for _ in vector]
    return [float(x) / norm for x in vector]


def index_ready(index_dir: str, roots: Sequence[Path]) -> bool:
    meta_path = Path(index_dir).expanduser() / "meta.json"
    vec_path = Path(index_dir).expanduser() / "vectors.f32"
    if not meta_path.is_file() or not vec_path.is_file():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return meta.get("version") == INDEX_VERSION and meta.get("signature") == _signature(roots)


def build_index(index_dir: str, roots: Sequence[Path], client: Any, *,
                batch_size: int = 32) -> Optional[str]:
    """Build or refresh the index; returns the vector path or None."""
    usable = [Path(r) for r in roots if Path(r).is_dir()]
    if not usable or not getattr(client, "configured", False):
        return None
    out = Path(index_dir).expanduser()
    vec_path = out / "vectors.f32"
    meta_path = out / "meta.json"
    chunks: List[Dict[str, str]] = []
    for chunk in _iter_chunks(usable):
        chunks.append(chunk)
        if len(chunks) >= MAX_INDEX_CHUNKS:
            break
    if not chunks:
        return None
    vectors: List[List[float]] = []
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start:start + batch_size]
        embedded = client.embed([c["text"] for c in batch], timeout=120.0)
        if not embedded:
            return None
        vectors.extend(embedded)
    if len(vectors) != len(chunks):
        return None
    dim = len(vectors[0])
    if dim == 0 or any(len(row) != dim for row in vectors):
        return None
    flat = array.array("f")
    for row in vectors:
        flat.extend(_normalize(row))
    out.mkdir(parents=True, exist_ok=True)
    with vec_path.open("wb") as handle:
        flat.tofile(handle)
    meta = {
        "version": INDEX_VERSION,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "signature": _signature(usable),
        "dim": dim,
        "count": len(chunks),
        "digest": hashlib.sha256(json.dumps(chunks, ensure_ascii=False).encode("utf-8")).hexdigest()[:16],
        "chunks": chunks,
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return str(vec_path)


#: Vectors are a multi-megabyte file; reloading them on every turn would cost
#: more than the query itself. Cache on (path, mtime, size) and reload when the
#: index is rebuilt.
_CACHE: Dict[str, Any] = {"key": None, "matrix": None, "meta": None, "dim": 0}


def _load(index_dir: str) -> Tuple[Optional[array.array], Optional[Dict[str, Any]], int]:
    out = Path(index_dir).expanduser()
    meta_path = out / "meta.json"
    vec_path = out / "vectors.f32"
    if not meta_path.is_file() or not vec_path.is_file():
        return None, None, 0
    try:
        stat = vec_path.stat()
        key = (str(vec_path), stat.st_mtime_ns, stat.st_size)
        if _CACHE.get("key") == key and _CACHE.get("matrix") is not None:
            return _CACHE["matrix"], _CACHE["meta"], int(_CACHE["dim"])
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("version") != INDEX_VERSION:
            return None, None, 0
        dim = int(meta.get("dim", 0))
        count = int(meta.get("count", 0))
        matrix = array.array("f")
        with vec_path.open("rb") as handle:
            matrix.fromfile(handle, count * dim)
        _CACHE.update({"key": key, "matrix": matrix, "meta": meta, "dim": dim})
        return matrix, meta, dim
    except (OSError, ValueError, EOFError):
        return None, None, 0


def retrieve(query: str, index_dir: str, client: Any, *, top_k: int = DEFAULT_TOP_K,
             min_score: float = DEFAULT_MIN_SCORE) -> List[Dict[str, Any]]:
    if not query.strip() or not getattr(client, "configured", False):
        return []
    matrix, meta, dim = _load(index_dir)
    if matrix is None or meta is None or dim <= 0:
        return []
    vector = client.embed([query], timeout=30.0)
    if not vector or not vector[0] or len(vector[0]) != dim:
        return []
    q = array.array("f", _normalize(vector[0]))
    view = memoryview(matrix)
    count = int(meta.get("count", 0))
    scores = [sum(map(operator.mul, view[i * dim:(i + 1) * dim], q)) for i in range(count)]
    order = sorted(range(count), key=scores.__getitem__, reverse=True)
    chunks = meta.get("chunks") or []
    hits: List[Dict[str, Any]] = []
    per_file: Dict[str, int] = {}
    for idx in order:
        if len(hits) >= top_k:
            break
        score = float(scores[idx])
        if score < min_score:
            break
        if idx >= len(chunks):
            continue
        row = chunks[idx]
        path = str(row.get("path", ""))
        if per_file.get(path, 0) >= _MAX_CHUNKS_PER_FILE:
            continue
        per_file[path] = per_file.get(path, 0) + 1
        hits.append({"path": path, "heading": str(row.get("heading", "")),
                     "date": str(row.get("date", "")), "score": score,
                     "text": str(row.get("text", ""))})
    return hits


def format_block(hits: Sequence[Dict[str, Any]], *, max_chars: int = MAX_BLOCK_CHARS) -> str:
    if not hits:
        return ""
    header = (
        "### 先例检索（仓内历史结论，非当前真相）\n"
        "以下片段按相似度召回，均带日期与来源。旧结论可能已被后续修改推翻：\n"
        "引用前必须用当前代码或最新 handover 复核，不得把历史现场当成现状。"
    )
    body: List[str] = []
    used = len(header)
    for hit in hits:
        stamp = f"[{hit.get('date')}]" if hit.get("date") else "[未标注日期]"
        flat = re.sub(r"\s+", " ", str(hit.get("text", ""))).strip()[:200]
        line = (f"- {stamp} `{hit.get('path')}` §{hit.get('heading') or '(无标题)'} "
                f"(score={float(hit.get('score', 0.0)):.3f})\n  {flat}")
        if used + len(line) > max_chars:
            break
        body.append(line)
        used += len(line)
    return header + "\n" + "\n".join(body) if body else ""
