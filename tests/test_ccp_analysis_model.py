import json
from urllib.error import URLError, HTTPError
import pytest
from agentcore.ccp_config import CcpCredentials
from agentcore import ccp_analysis_model as adapter

class Reply:
    def __init__(self, body): self.body=body
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def read(self,n=-1): return self.body[:n]

def provider_reply(content="ok", **extra):
    message={"role":"assistant","content":content,**extra}
    return json.dumps({"choices":[{"message":message,"finish_reason":"stop"}]}).encode()

def configure(monkeypatch, body=None, base="http://127.0.0.1:4000/v1"):
    captured={}
    monkeypatch.setattr(adapter,"resolve_ccp_credentials",lambda **kw: CcpCredentials(base,kw["alias"],"secret-do-not-print","test"))
    def fake_open(request,timeout):
        captured["url"]=request.full_url
        captured["auth"]=request.get_header("Authorization")
        captured["payload"]=json.loads(request.data)
        captured["timeout"]=timeout
        return Reply(body if body is not None else provider_reply("```json\n{}\n```"))
    monkeypatch.setattr(adapter,"_open_request",fake_open)
    return captured

def test_default_flash_uses_tool_free_completion_and_returns_text(monkeypatch):
    seen=configure(monkeypatch)
    result=adapter.model([{"role":"system","content":"rules"},{"role":"user","content":"synthetic"}])
    assert result=="```json\n{}\n```"
    assert seen["url"]=="http://127.0.0.1:4000/v1/chat/completions"
    assert seen["payload"]=={"model":"flash","messages":[{"role":"system","content":"rules"},{"role":"user","content":"synthetic"}],"stream":False}
    assert "tools" not in seen["payload"] and "functions" not in seen["payload"]
    assert seen["auth"]=="Bearer secret-do-not-print"
    assert "secret-do-not-print" not in repr(seen["payload"])

def test_admin_can_select_only_main_or_flash(monkeypatch):
    seen=configure(monkeypatch)
    adapter.model([{"role":"user","content":"x"}],alias="main",timeout_seconds=5)
    assert seen["payload"]["model"]=="main" and seen["timeout"]==5
    for alias in ("pro","http://attacker",None):
        with pytest.raises(adapter.CcpAnalysisModelError,match="invalid_alias"):
            adapter.model([{"role":"user","content":"x"}],alias=alias)

def test_rejects_tool_calls_and_malformed_provider_response(monkeypatch):
    configure(monkeypatch,json.dumps({"choices":[{"message":{"content":"no","tool_calls":[{"id":"x"}]},"finish_reason":"tool_calls"}]}).encode())
    with pytest.raises(adapter.CcpAnalysisModelError,match="tool_response_rejected"):
        adapter.model([{"role":"user","content":"x"}])
    configure(monkeypatch,b"not-json")
    with pytest.raises(adapter.CcpAnalysisModelError,match="invalid_provider_response"):
        adapter.model([{"role":"user","content":"x"}])

def test_errors_are_redacted_and_timeout_is_bounded(monkeypatch):
    configure(monkeypatch)
    def fail(request,timeout): raise URLError("Bearer secret-do-not-print")
    monkeypatch.setattr(adapter,"_open_request",fail)
    with pytest.raises(adapter.CcpAnalysisModelError) as error:
        adapter.model([{"role":"user","content":"x"}],timeout_seconds=7)
    assert str(error.value)=="ccp_request_failed" and "secret" not in str(error.value)
    for value in (0,301,True,"5"):
        with pytest.raises(adapter.CcpAnalysisModelError,match="invalid_timeout"):
            adapter.model([{"role":"user","content":"x"}],timeout_seconds=value)

def test_rejects_non_loopback_endpoint_and_credentials_in_url(monkeypatch):
    for endpoint in ("http://example.com/v1","https://127.0.0.1/v1","http://user:pass@127.0.0.1/v1","http://127.0.0.1/v1?x=y"):
        configure(monkeypatch,base=endpoint)
        with pytest.raises(adapter.CcpAnalysisModelError,match="unsafe_ccp_endpoint"):
            adapter.model([{"role":"user","content":"x"}])

def test_rejects_invalid_message_and_large_body(monkeypatch):
    configure(monkeypatch)
    with pytest.raises(adapter.CcpAnalysisModelError,match="invalid_messages"):
        adapter.model([{"role":"tool","content":"x"}])
    configure(monkeypatch,b" "*(adapter._MAX_RESPONSE_BYTES+1))
    with pytest.raises(adapter.CcpAnalysisModelError,match="response_too_large"):
        adapter.model([{"role":"user","content":"x"}])


def test_transport_disables_proxy_and_redirect(monkeypatch):
    from urllib.request import ProxyHandler
    captured={}
    class Opener:
        def open(self, request, timeout):
            captured["timeout"]=timeout
            raise URLError("redirect denied")
    def build(*handlers):
        captured["handlers"]=handlers
        return Opener()
    monkeypatch.setattr(adapter,"build_opener",build)
    from urllib.request import Request
    with pytest.raises(URLError):
        adapter._open_request(Request("http://127.0.0.1/v1/chat/completions"),3)
    proxy=next(item for item in captured["handlers"] if isinstance(item,ProxyHandler))
    assert proxy.proxies=={}
    redirect=next(item for item in captured["handlers"] if isinstance(item,adapter._NoRedirect))
    assert redirect.redirect_request(None,None,302,"Found",{},"http://example.invalid/") is None
    assert captured["timeout"]==3

def test_credential_resolver_errors_are_redacted(monkeypatch):
    def unavailable(**kwargs): raise RuntimeError("api key secret-do-not-print")
    monkeypatch.setattr(adapter,"resolve_ccp_credentials",unavailable)
    with pytest.raises(adapter.CcpAnalysisModelError) as error:
        adapter.model([{"role":"user","content":"x"}])
    assert str(error.value)=="ccp_credentials_unavailable"
    assert "secret" not in str(error.value)

def test_truncated_model_output_is_rejected(monkeypatch):
    body=json.dumps({"choices":[{"message":{"content":"partial"},"finish_reason":"length"}]}).encode()
    configure(monkeypatch,body)
    with pytest.raises(adapter.CcpAnalysisModelError,match="provider_output_truncated"):
        adapter.model([{"role":"user","content":"x"}])
@pytest.mark.parametrize("bad",[b"[]",b"{}",b'{"choices":[]}',b'{"choices":[null]}',b'{"choices":[{"message":null}]}'])
def test_malformed_response_shapes_are_stable(monkeypatch,bad):
    configure(monkeypatch,bad)
    with pytest.raises(adapter.CcpAnalysisModelError,match="invalid_provider_response"):
        adapter.model([{"role":"user","content":"x"}])


@pytest.mark.parametrize("error,category", [
    (HTTPError("http://secret/path", 429, "secret-body", {}, None), "rate_limited"),
    (HTTPError("http://secret/path", 401, "secret-body", {}, None), "auth_rejected"),
    (HTTPError("http://secret/path", 503, "secret-body", {}, None), "provider_5xx"),
    (HTTPError("http://secret/path", 400, "secret-body", {}, None), "http_other"),
    (URLError(TimeoutError("secret-body")), "timeout"),
    (TimeoutError("secret-body"), "timeout"),
    (URLError("secret-body"), "connection"),
])
def test_transport_logs_only_coarse_failure_category(monkeypatch, caplog, error, category):
    configure(monkeypatch)
    def fail(request, timeout):
        raise error
    monkeypatch.setattr(adapter, "_open_request", fail)
    with pytest.raises(adapter.CcpAnalysisModelError, match="^ccp_request_failed$"):
        adapter.model([{"role": "user", "content": "secret-body"}])
    assert "CCP analysis model failure: " + category in caplog.text
    assert "secret" not in caplog.text


def test_invalid_provider_response_logs_only_safe_category(monkeypatch, caplog):
    configure(monkeypatch, b"secret-provider-body")
    with pytest.raises(adapter.CcpAnalysisModelError, match="invalid_provider_response"):
        adapter.model([{"role": "user", "content": "private prompt"}])
    assert "CCP analysis model failure: invalid_provider_response" in caplog.text
    assert "secret-provider-body" not in caplog.text
    assert "private prompt" not in caplog.text
