"""Tests for agentcore.preflight check module."""

from __future__ import annotations

from unittest.mock import patch
import pytest

from agentcore.preflight import (
    CheckItemResult,
    PreflightReport,
    check_disk_space,
    check_dns_latency,
    check_feishu_api_latency,
    check_idempotent_store_latency,
    check_tcp_port_available,
    run_preflight_suite,
)


class FakeResponse:
    """Minimal urlopen() context-manager stand-in; only .status is read."""

    def __init__(self, status: int):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_check_dns_latency_success():
    with patch("socket.gethostbyname", return_value="127.0.0.1"):
        res = check_dns_latency(target_host="localhost", max_acceptable_ms=500.0)
        assert res.passed is True
        assert "127.0.0.1" in res.message


def test_check_dns_latency_timeout():
    with patch("socket.gethostbyname", side_effect=Exception("Timeout")):
        res = check_dns_latency(target_host="invalid.local.test")
        assert res.passed is False
        assert "失败" in res.message or "Timeout" in res.message


def test_check_feishu_api_retries_transient_connection_refusal():
    """代理端口瞬断（Connection refused）重试一次后能连上就不该判失败。"""
    import errno
    import urllib.error

    refused = urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"))
    response = FakeResponse(200)
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        if len(calls) == 1:
            raise refused
        return response

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        res = check_feishu_api_latency(max_acceptable_ms=800.0)
    assert res.passed is True
    assert len(calls) == 2
    assert "200" in res.message


def test_check_feishu_api_does_not_retry_timeout():
    """超时属于被本门禁捕获的目标，不能靠重试洗成通过。"""
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        raise TimeoutError("timed out")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        res = check_feishu_api_latency(max_acceptable_ms=800.0)
    assert res.passed is False
    assert len(calls) == 1
    assert "连接失败" in res.message


def test_check_feishu_api_gives_up_after_all_attempts_refused():
    """持续拒连仍然判失败，重试不是豁免。"""
    import errno
    import urllib.error

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"))

    with patch("urllib.request.urlopen", side_effect=fake_urlopen) as mocked:
        res = check_feishu_api_latency(max_acceptable_ms=800.0)
    assert res.passed is False
    assert mocked.call_count == 2


def test_check_feishu_api_still_enforces_latency_budget_on_success():
    """重试只修瞬断，不放松延迟预算：慢但连上的请求仍判失败。"""
    with patch("urllib.request.urlopen", return_value=FakeResponse(200)):
        res = check_feishu_api_latency(max_acceptable_ms=0.000001)
    assert res.passed is False
    assert "严重超时" in res.message


def test_check_disk_space():
    res = check_disk_space(path="/", min_free_gb=0.1)
    assert res.passed is True
    assert "可用空间" in res.message


def test_check_tcp_port_available():
    res = check_tcp_port_available(port=65534)
    assert res.passed is True
    assert "端口状态" in res.message


def test_check_idempotent_store_latency(tmp_path):
    res = check_idempotent_store_latency(test_dir=tmp_path, max_acceptable_ms=50.0)
    assert res.passed is True
    assert "I/O 往返" in res.message


def test_check_feishu_api_latency_success():
    with patch("urllib.request.urlopen") as mock_open:
        mock_resp = mock_open.return_value.__enter__.return_value
        mock_resp.status = 200
        res = check_feishu_api_latency(max_acceptable_ms=1000.0)
        assert res.passed is True
        assert "HTTP 往返" in res.message


def test_check_feishu_api_latency_timeout():
    with patch("urllib.request.urlopen", side_effect=Exception("Connection timeout")):
        res = check_feishu_api_latency()
        assert res.passed is False
        assert "连接失败" in res.message


def test_run_preflight_suite(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "no-hermes"))
    with patch("socket.gethostbyname", return_value="1.2.3.4"):
        # Explicit clean baseline config to isolate test from host environment
        cfg_file = tmp_path / "config.yaml"
        import yaml
        from agentcore.baseline_config import CORE_RUNTIME_BASELINE
        cfg_file.write_text(yaml.dump(CORE_RUNTIME_BASELINE), encoding="utf-8")
        report = run_preflight_suite(probe_host="localhost", min_free_gb=0.1, config_path=cfg_file)
        assert report.all_passed is True
        summary_text = report.summary()
        assert "[OK]" in summary_text
        assert "PASS" in summary_text

