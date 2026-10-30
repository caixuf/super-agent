"""Read-only CCP chat-completions adapter for agentcore.analysis_runner."""
from __future__ import annotations

import ipaddress
import json
import logging
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

from agentcore.ccp_config import resolve_ccp_credentials


class CcpAnalysisModelError(RuntimeError):
    """Stable error code; never includes credential or provider response text."""


_MAX_RESPONSE_BYTES = 512_000
_logger = logging.getLogger(__name__)

class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None

def _open_request(request, timeout):
    # No environment proxies: Authorization stays on the loopback request.
    return build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout)


def _safe_endpoint(base_url: str) -> str:
    try:
        parsed = urlsplit(base_url)
        if (parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ValueError
        host = parsed.hostname.lower().rstrip(".")
        if host == "localhost":
            pass
        elif not ipaddress.ip_address(host).is_loopback:
            raise ValueError
        port = parsed.port
        if port is None:
            port = 80
        if not 1 <= port <= 65535:
            raise ValueError
        path = parsed.path.rstrip("/")
        if path != "/v1":
            raise ValueError
        if host == "localhost":
            addresses = {item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
            if not addresses or any(not ipaddress.ip_address(address).is_loopback for address in addresses):
                raise ValueError
        formatted_host = "[" + host + "]" if ":" in host else host
        return urlunsplit(("http", f"{formatted_host}:{port}", path + "/chat/completions", "", ""))
    except (OSError, TypeError, ValueError):
        raise CcpAnalysisModelError("unsafe_ccp_endpoint") from None


def _positive_timeout(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= 300:
        raise CcpAnalysisModelError("invalid_timeout")
    return float(value)


def _messages(messages) -> list[dict[str, str]]:
    if not isinstance(messages, list) or not messages:
        raise CcpAnalysisModelError("invalid_messages")
    result = []
    for item in messages:
        if (not isinstance(item, dict) or item.get("role") not in {"system", "user", "assistant"}
                or not isinstance(item.get("content"), str)):
            raise CcpAnalysisModelError("invalid_messages")
        result.append({"role": item["role"], "content": item["content"]})
    return result


def _response(body: bytes) -> str:
    if len(body) > _MAX_RESPONSE_BYTES:
        raise CcpAnalysisModelError("response_too_large")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise CcpAnalysisModelError("invalid_provider_response") from None
    if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or not data["choices"]:
        raise CcpAnalysisModelError("invalid_provider_response")
    choice = data["choices"][0]
    if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
        raise CcpAnalysisModelError("invalid_provider_response")
    message = choice["message"]
    if choice.get("finish_reason") == "length":
        raise CcpAnalysisModelError("provider_output_truncated")
    if choice.get("finish_reason") == "tool_calls" or message.get("tool_calls") or message.get("function_call"):
        raise CcpAnalysisModelError("tool_response_rejected")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip() or len(content.encode("utf-8")) > _MAX_RESPONSE_BYTES:
        raise CcpAnalysisModelError("invalid_provider_response")
    return content
def model(messages, **options):
    """Run one tool-free CCP completion; default logical route is ``flash``.

    Trusted callers may pass ``alias`` (``main`` or ``flash``) and
    ``timeout_seconds``. The model sees only chat text; the request contains no
    tool/function declarations. Credential discovery is delegated to CCP's
    existing resolver and no secret is included in argv or error messages.
    """
    if set(options) - {"alias", "timeout_seconds"}:
        raise CcpAnalysisModelError("invalid_options")
    alias = options.get("alias", "flash")
    if alias not in {"main", "flash"}:
        raise CcpAnalysisModelError("invalid_alias")
    timeout = _positive_timeout(options.get("timeout_seconds", 120))
    chat = _messages(messages)
    try:
        credentials = resolve_ccp_credentials(alias=alias)
        url = _safe_endpoint(credentials.base_url)
        payload = json.dumps({"model": credentials.alias, "messages": chat, "stream": False},
                             ensure_ascii=False).encode("utf-8")
        request = Request(url, data=payload, method="POST", headers={
            "Authorization": "Bearer " + credentials.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        with _open_request(request, timeout) as response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
        return _response(body)
    except CcpAnalysisModelError as exc:
        code = str(exc)
        allowed = {"unsafe_ccp_endpoint", "response_too_large",
                   "invalid_provider_response", "provider_output_truncated",
                   "tool_response_rejected"}
        _logger.warning("CCP analysis model failure: %s",
                        code if code in allowed else "adapter_error")
        raise
    except RuntimeError:
        _logger.warning("CCP analysis model failure: credentials_unavailable")
        raise CcpAnalysisModelError("ccp_credentials_unavailable") from None
    except HTTPError as exc:
        # Only an explicit allowlist of categories: never log URL, headers or body.
        category = ("rate_limited" if exc.code == 429 else
                    "auth_rejected" if exc.code in (401, 403) else
                    "provider_5xx" if isinstance(exc.code, int) and 500 <= exc.code <= 599 else
                    "http_other")
        _logger.warning("CCP analysis model failure: %s", category)
        raise CcpAnalysisModelError("ccp_request_failed") from None
    except (TimeoutError, socket.timeout):
        _logger.warning("CCP analysis model failure: timeout")
        raise CcpAnalysisModelError("ccp_request_failed") from None
    except URLError as exc:
        category = "timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "connection"
        _logger.warning("CCP analysis model failure: %s", category)
        raise CcpAnalysisModelError("ccp_request_failed") from None
    except (OSError, ValueError):
        _logger.warning("CCP analysis model failure: connection")
        raise CcpAnalysisModelError("ccp_request_failed") from None
