"""Isolated durable queue -> analysis_runner -> CCP adapter integration."""
import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import sys
import threading

from agentcore.worker import Worker

REPO = Path(__file__).resolve().parents[1]
PLUGIN_PATH = REPO / "plugins/durable_dispatch/__init__.py"
SPEC = importlib.util.spec_from_file_location("durable_dispatch_ccp_integration", PLUGIN_PATH)
PLUGIN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLUGIN)

class CCPHandler(BaseHTTPRequestHandler):
    requests = []
    invalid = False
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        request = json.loads(body)
        type(self).requests.append({"path": self.path, "auth_ok": self.headers.get("Authorization") == "Bearer test-only-key",
                                   "request": request})
        if type(self).invalid:
            answer = {"choices": [{"message": {"content": json.dumps({
                "conclusion": "bad", "claims": [{"statement": "unbacked", "evidence": [
                    {"source_id": "source_1", "line": 1, "quote": "invented"}]}],
                "limitations": ["synthetic"]})}, "finish_reason": "stop"}]}
        else:
            answer = {"choices": [{"message": {"content": json.dumps({
                "conclusion": "The fixture records a synthetic warning.", "claims": [{
                    "statement": "The log contains a warning.", "evidence": [
                        {"source_id": "source_1", "line": 1, "quote": "synthetic warning"}]}],
                "limitations": ["Synthetic fixture only."]})}, "finish_reason": "stop"}]}
        encoded = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
    def log_message(self, *args):
        pass

def run_pipeline(tmp_path, monkeypatch, *, invalid=False):
    CCPHandler.requests = []
    CCPHandler.invalid = invalid
    server = ThreadingHTTPServer(("127.0.0.1", 0), CCPHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        sources = tmp_path / "approved-logs"
        sources.mkdir()
        log = sources / "synthetic.log"
        log.write_text("synthetic warning: retry budget reached\n", encoding="utf-8")
        config = tmp_path / "hermes-provider.yaml"
        config.write_text("providers:\n  litellm:\n    base_url: http://127.0.0.1:%d/v1\n    key_env: CCP_TEST_KEY\n" % server.server_port,
                          encoding="utf-8")
        policy = tmp_path / "analysis-policy.json"
        policy.write_text(json.dumps({"allowed_roots": [str(sources)],
                                      "model_options": {"alias": "flash", "timeout_seconds": 10}}),
                          encoding="utf-8")
        monkeypatch.setenv("PI_EXECUTOR_HERMES_CONFIG", str(config))
        monkeypatch.setenv("CCP_TEST_KEY", "test-only-key")
        monkeypatch.delenv("PI_CCP_API_KEY", raising=False)
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(REPO), os.environ.get("PYTHONPATH", "")]))
        service = PLUGIN.DispatchService(str(tmp_path / "queue"), "test-profile", [str(sources)])
        submitted = service.submit("session-a", {"request_key": "fixture-1", "goal": "Explain the warning",
                                                  "sources": [{"path": str(log)}]})
        assert submitted["status"] == "queued"
        command = [sys.executable, str(REPO / "agentcore/analysis_runner.py"), "--policy", str(policy),
                   "--model-adapter", "agentcore.ccp_analysis_model:model"]
        stats = Worker(service.pool, command, workers=1, timeout=30, poll_interval=.02).run(drain=True)
        return service.status("session-a", {"task_id": submitted["task_id"]}), stats, list(CCPHandler.requests)
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

def test_durable_dispatch_analysis_runner_uses_configured_flash(tmp_path, monkeypatch):
    status, stats, requests = run_pipeline(tmp_path, monkeypatch)
    assert status["status"] == "completed"
    assert stats["attempts_finished"] == 1
    assert len(requests) == 1
    sent = requests[0]
    assert sent["path"] == "/v1/chat/completions" and sent["auth_ok"]
    assert sent["request"]["model"] == "flash"
    assert "tools" not in sent["request"] and "functions" not in sent["request"]
    result = status["result"]["result"]
    assert result["verification"] == "citation_traceability_only"
    manifest = json.loads(Path(result["artifacts"][2]).read_text(encoding="utf-8"))
    assert manifest["sources"][0]["lines"] == [{"line": 1, "text": "synthetic warning: retry budget reached"}]

def test_unverified_evidence_fails_visibly_through_durable_status(tmp_path, monkeypatch):
    status, _, requests = run_pipeline(tmp_path, monkeypatch, invalid=True)
    assert len(requests) == 2  # Existing read-only queue retry policy.
    assert status["status"] == "failed"
    assert status["result"]["runner_error"] == "unverified_citation"
