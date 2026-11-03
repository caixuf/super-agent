"""Tests for agentcore.preflight check module."""

from __future__ import annotations

from unittest.mock import patch
import pytest

from agentcore.preflight import (
    CheckItemResult,
    PreflightReport,
    check_disk_space,
    check_dns_latency,
    check_tcp_port_available,
    run_preflight_suite,
)


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


def test_check_disk_space():
    res = check_disk_space(path="/", min_free_gb=0.1)
    assert res.passed is True
    assert "可用空间" in res.message


def test_check_tcp_port_available():
    res = check_tcp_port_available(port=65534)
    assert res.passed is True
    assert "端口状态" in res.message


def test_run_preflight_suite(tmp_path, monkeypatch):
    # Isolate from the host Hermes profile so baseline drift cannot fail CI/dev.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "no-hermes"))
    with patch("socket.gethostbyname", return_value="1.2.3.4"):
        report = run_preflight_suite(probe_host="localhost", min_free_gb=0.1)
        assert report.all_passed is True
        summary_text = report.summary()
        assert "[OK]" in summary_text
        assert "PASS" in summary_text

