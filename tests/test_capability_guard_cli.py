"""Offline suite for the capability-guard operator CLI.

Every live-mode request targets a loopback ``http.server`` started in a daemon
thread; no external network is used. Dry-run cases assert that nothing durable
or network-visible happens.
"""
from __future__ import annotations

import io
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agentcore.capability_guard_cli import (
    EXIT_DELIVERY,
    EXIT_INPUT,
    EXIT_NO_TARGET,
    EXIT_OK,
    main,
)
from agentcore.ops_alerts import AlertOutbox


def healthy_patrol() -> dict:
    return {
        "ok": True,
        "autoheal": True,
        "baselines": {"agent.max_turns": 1000},
        "observed": [{"key": "agent.max_turns", "value": 1000, "floor": 1000}],
        "healed": [],
        "alerts": [],
    }


def drift_patrol() -> dict:
    return {
        "ok": True,
        "autoheal": False,
        "baselines": {"agent.max_turns": 1000},
        "observed": [{"key": "agent.max_turns", "value": 15, "floor": 1000}],
        "healed": [],
        "alerts": ["agent.max_turns = 15 < 基线 1000"],
    }


class LocalWebhook:
    """Record every POST and answer with one fixed status/body."""

    def __init__(self, status: int = 202, body: bytes = b'{"receipt_id": "rcpt-local"}'):
        self.requests: list[dict] = []
        record = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                record.requests.append({
                    "path": self.path,
                    "body": self.rfile.read(length),
                    "idempotency_key": self.headers.get("Idempotency-Key"),
                })
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()

            def log_message(self, *args):
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.url = "http://127.0.0.1:%d/alerts" % self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self._thread.start()

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2)


@pytest.fixture
def webhooks():
    started = []

    def start(status: int = 202, body: bytes = b'{"receipt_id": "rcpt-local"}'):
        server = LocalWebhook(status=status, body=body)
        started.append(server)
        return server

    yield start
    for server in started:
        server.close()


def write_patrol(tmp_path, payload) -> str:
    path = tmp_path / "patrol.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def read_summary(capsys) -> dict:
    captured = capsys.readouterr()
    assert captured.out, "CLI printed no summary"
    return json.loads(captured.out)


@pytest.mark.parametrize("raw", ["{not json", "[]", '{"alerts": {}}', '"text"'])
def test_malformed_input_exits_2_without_touching_the_queue(tmp_path, capsys, raw):
    path = tmp_path / "patrol.json"
    path.write_text(raw, encoding="utf-8")
    root = tmp_path / "queue"

    code = main(["--patrol-json", str(path), "--profile", "p", "--scope", "s",
                 "--queue-root", str(root), "--no-dry-run"])

    assert code == EXIT_INPUT
    assert not root.exists()
    summary = json.loads(capsys.readouterr().err)
    assert summary["status"] == "error"
    assert summary["exit_code"] == EXIT_INPUT
    assert summary["error"] == "input"


def test_missing_patrol_file_exits_2(tmp_path, capsys):
    code = main(["--patrol-json", str(tmp_path / "absent.json"),
                 "--profile", "p", "--scope", "s"])
    assert code == EXIT_INPUT
    assert "cannot read patrol JSON" in capsys.readouterr().err


def test_dry_run_is_default_and_never_creates_the_outbox(tmp_path, capsys, webhooks):
    server = webhooks()
    root = tmp_path / "queue"

    code = main(["--patrol-json", write_patrol(tmp_path, drift_patrol()),
                 "--profile", "p", "--scope", "s", "--queue-root", str(root),
                 "--webhook-url", server.url])

    assert code == EXIT_OK
    assert not root.exists()
    assert server.requests == []
    summary = read_summary(capsys)
    assert summary["dry_run"] is True
    assert summary["status"] == "preview"
    assert summary["drifted"] is True
    assert summary["would_deliver"] is True
    assert "【能力地板告警】p/s" in summary["message"]
    assert summary["exit_code"] == EXIT_OK


def test_dry_run_quiet_patrol_renders_no_message(tmp_path, capsys):
    code = main(["--patrol-json", write_patrol(tmp_path, healthy_patrol()),
                 "--profile", "p", "--scope", "s"])

    assert code == EXIT_OK
    summary = read_summary(capsys)
    assert summary["status"] == "quiet"
    assert summary["drifted"] is False
    assert summary["message"] is None


def test_dry_run_accepts_preview_overrides(tmp_path, capsys):
    code = main(["--patrol-json", write_patrol(tmp_path, drift_patrol()),
                 "--profile", "p", "--scope", "s",
                 "--config-persisted", "--runtime-loaded"])

    assert code == EXIT_OK
    summary = read_summary(capsys)
    assert summary["config_persisted"] is True
    assert summary["runtime_loaded"] is True
    assert "重启网关后生效" in summary["message"]


def test_live_quiet_patrol_exits_0(tmp_path, capsys):
    root = tmp_path / "queue"

    code = main(["--patrol-json", write_patrol(tmp_path, healthy_patrol()),
                 "--profile", "p", "--scope", "s", "--queue-root", str(root),
                 "--no-dry-run"])

    assert code == EXIT_OK
    summary = read_summary(capsys)
    assert summary["status"] == "quiet"
    assert summary["pending_count"] == 0
    assert AlertOutbox(str(root)).pending() == []


def test_live_delivers_to_loopback_webhook(tmp_path, capsys, webhooks):
    server = webhooks()
    root = tmp_path / "queue"

    code = main(["--patrol-json", write_patrol(tmp_path, drift_patrol()),
                 "--profile", "p", "--scope", "s", "--queue-root", str(root),
                 "--webhook-url", server.url, "--token", "local-secret",
                 "--no-dry-run"])

    assert code == EXIT_OK
    assert len(server.requests) == 1
    request = server.requests[0]
    assert request["path"] == "/alerts"
    payload = json.loads(request["body"])
    assert payload["kind"] == "capability_drift"
    assert payload["profile"] == "p"
    assert payload["scope"] == "s"
    assert request["idempotency_key"] == payload["event_id"]
    assert AlertOutbox(str(root)).pending() == []
    captured = capsys.readouterr()
    assert "local-secret" not in captured.out
    summary = json.loads(captured.out)
    assert summary["status"] == "delivered"
    assert summary["delivery"]["status"] == "delivered"
    assert summary["pending_count"] == 0


def test_live_without_url_leaves_pending_and_exits_3(tmp_path, capsys):
    root = tmp_path / "queue"

    code = main(["--patrol-json", write_patrol(tmp_path, drift_patrol()),
                 "--profile", "p", "--scope", "s", "--queue-root", str(root),
                 "--no-dry-run"])

    assert code == EXIT_NO_TARGET
    pending = AlertOutbox(str(root)).pending()
    assert len(pending) == 1
    assert pending[0]["event"]["profile"] == "p"
    summary = read_summary(capsys)
    assert summary["status"] == "pending"
    assert summary["webhook_configured"] is False
    assert summary["pending_count"] == 1


def test_live_http_500_leaves_pending_and_exits_4(tmp_path, capsys, webhooks):
    server = webhooks(status=500, body=b'{"error": "boom"}')
    root = tmp_path / "queue"

    code = main(["--patrol-json", write_patrol(tmp_path, drift_patrol()),
                 "--profile", "p", "--scope", "s", "--queue-root", str(root),
                 "--webhook-url", server.url, "--no-dry-run"])

    assert code == EXIT_DELIVERY
    assert len(server.requests) == 1
    pending = AlertOutbox(str(root)).pending()
    assert len(pending) == 1
    assert pending[0]["attempts"] == 1
    summary = read_summary(capsys)
    assert summary["status"] == "pending"
    assert summary["delivery"]["status"] == "delivery_unknown"
    assert summary["exit_code"] == EXIT_DELIVERY


def test_live_contradicting_override_exits_2_and_writes_nothing(tmp_path, capsys):
    root = tmp_path / "queue"

    code = main(["--patrol-json", write_patrol(tmp_path, drift_patrol()),
                 "--profile", "p", "--scope", "s", "--queue-root", str(root),
                 "--config-persisted", "--no-dry-run"])

    assert code == EXIT_INPUT
    assert not root.exists()
    assert "contradicts the patrol" in capsys.readouterr().err


def test_invalid_webhook_configuration_exits_2(tmp_path, capsys):
    code = main(["--patrol-json", write_patrol(tmp_path, drift_patrol()),
                 "--profile", "p", "--scope", "s",
                 "--webhook-url", "ftp://example.invalid/alerts"])
    assert code == EXIT_INPUT
    assert "invalid webhook configuration" in capsys.readouterr().err


def test_stdin_patrol_source_is_supported(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(healthy_patrol())))

    code = main(["--patrol-json", "-", "--profile", "p", "--scope", "s"])

    assert code == EXIT_OK
    assert read_summary(capsys)["status"] == "quiet"
