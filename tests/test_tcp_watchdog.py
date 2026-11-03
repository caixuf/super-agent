"""Tests for agentcore.tcp_watchdog."""

from __future__ import annotations

import pytest
from agentcore.tcp_watchdog import (
    SocketInfo,
    TcpHalfOpenWatchdog,
    TcpHangState,
)


def test_tcp_watchdog_healthy():
    watchdog = TcpHalfOpenWatchdog(max_send_q_stuck_s=5.0)
    # Healthy socket: send_q == 0
    sockets = [
        SocketInfo(local_addr="127.0.0.1:4000", peer_addr="127.0.0.1:5000", send_q=0, recv_q=0)
    ]
    state = watchdog.evaluate_sockets(sockets, now=100.0)
    assert state.is_hung is False
    assert state.stuck_send_q == 0


def test_tcp_watchdog_temporary_spurt():
    watchdog = TcpHalfOpenWatchdog(max_send_q_stuck_s=5.0)
    # Temporary buffer spurt at t=100
    sockets_stuck = [
        SocketInfo(local_addr="127.0.0.1:42140", peer_addr="127.0.0.1:443", send_q=441, recv_q=0)
    ]
    state1 = watchdog.evaluate_sockets(sockets_stuck, now=100.0)
    assert state1.is_hung is False
    assert state1.stuck_send_q == 441

    # Quickly cleared at t=102
    sockets_cleared = [
        SocketInfo(local_addr="127.0.0.1:42140", peer_addr="127.0.0.1:443", send_q=0, recv_q=0)
    ]
    state2 = watchdog.evaluate_sockets(sockets_cleared, now=102.0)
    assert state2.is_hung is False
    assert state2.stuck_send_q == 0


def test_tcp_watchdog_detects_half_open_hang():
    watchdog = TcpHalfOpenWatchdog(max_send_q_stuck_s=5.0)
    sockets_stuck = [
        SocketInfo(local_addr="127.0.0.1:42140", peer_addr="127.0.0.1:443", send_q=500, recv_q=0)
    ]

    # First check at t=100 (duration 0s)
    state1 = watchdog.evaluate_sockets(sockets_stuck, now=100.0)
    assert state1.is_hung is False

    # Check at t=103 (duration 3s < 5s)
    state2 = watchdog.evaluate_sockets(sockets_stuck, now=103.0)
    assert state2.is_hung is False

    # Check at t=106 (duration 6s > 5s -> HUNG)
    state3 = watchdog.evaluate_sockets(sockets_stuck, now=106.0)
    assert state3.is_hung is True
    assert state3.stuck_send_q == 500
    assert state3.duration_stuck_s == 6.0
    assert "Send-Q 持续积压" in state3.reason
