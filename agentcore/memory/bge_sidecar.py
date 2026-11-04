"""Bounded loopback adapter for a BGE-M3 dense embedding service."""
from __future__ import annotations
import ipaddress
import json
import math
import socket
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler


def checked_url(base):
    parsed = urlsplit(base)
    try:
        if (parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError
        host = parsed.hostname.lower()
        if host == "localhost":
            addresses = {x[4][0] for x in socket.getaddrinfo(host, parsed.port or 80, type=socket.SOCK_STREAM)}
            if not addresses or any(not ipaddress.ip_address(a).is_loopback for a in addresses):
                raise ValueError
        elif not ipaddress.ip_address(host).is_loopback:
            raise ValueError
        _ = parsed.port
    except (ValueError, OSError):
        raise ValueError("embedding endpoint must be a loopback HTTP origin") from None
    normalized = "127.0.0.1" if host == "localhost" else host
    if ":" in normalized:
        normalized = "[" + normalized + "]"
    port = (":" + str(parsed.port)) if parsed.port is not None else ""
    return "http://" + normalized + port + "/encode"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def make_embedder(base, dimension=1024, timeout=30):
    url = checked_url(base)
    if type(dimension) is not int or not 1 <= dimension <= 8192:
        raise ValueError("invalid embedding dimension")

    def embed(texts):
        if not isinstance(texts, (list, tuple)) or not 1 <= len(texts) <= 200:
            return None
        if any(not isinstance(t, str) or len(t) > 20000 for t in texts):
            return None
        body = json.dumps({"texts": list(texts), "return_dense": True,
                           "return_sparse": False, "return_colbert": False}).encode()
        request = Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout) as response:
                raw = response.read(16_000_001)
            if len(raw) > 16_000_000:
                return None
            rows = json.loads(raw).get("dense")
            if not isinstance(rows, list) or len(rows) != len(texts):
                return None
            if any(not isinstance(row, list) or len(row) != dimension or
                   any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
                       for x in row) for row in rows):
                return None
            return rows
        except Exception:
            # Backend errors do not expose input text or credentials.
            return None
    return embed
