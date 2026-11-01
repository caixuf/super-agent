from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from agentcore.memory.bge_sidecar import checked_url, make_embedder


def test_loopback_adapter_validates_shape_and_disables_redirect():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if data["texts"] == ["redirect"]:
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/leak")
                self.end_headers()
                return
            result = {"dense": [[1.0, 2.0] for _ in data["texts"]]}
            raw = json.dumps(result).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(raw)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        embed = make_embedder("http://127.0.0.1:%d" % server.server_port, dimension=2)
        assert embed(["a", "b"]) == [[1.0, 2.0], [1.0, 2.0]]
        assert embed(["redirect"]) is None
        assert make_embedder("http://127.0.0.1:%d" % server.server_port, dimension=3)(["a"]) is None
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.mark.parametrize("url", ["https://127.0.0.1:8910", "http://example.com", "http://user:secret@127.0.0.1"])
def test_nonlocal_or_credentialed_endpoints_are_rejected(url):
    with pytest.raises(ValueError):
        checked_url(url)
