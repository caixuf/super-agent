"""TCP 假死看门狗 (TCP Half-Open Hang Watchdog)。

【背景与痛点】：
长连接网关在网络波动或 NAT 闪断后，由于系统 TCP 重传退避默认需要 15 次重试 (约 15~20 分钟)，
进程误以为 socket 仍活着，但实际上对端早已断链，Send-Q 持续积压未获 ACK。
服务端判定客户端离线，用户新消息全部被压在云端，客户端陷入假死静默。

【防御原理】：
周期性 (如每 5 秒) 巡检长连接持有进程的 Socket 状态：
- 提取 Send-Q 积压字节数；
- 若 Send-Q > 0 且持续超过阈值时间 (如 10s)，主动判定为 TCP Half-Open 假死；
- 触发自愈回调 (重启长连接总线进程)，秒级恢复消息通道。
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class SocketInfo:
    local_addr: str
    peer_addr: str
    send_q: int
    recv_q: int
    pid: Optional[int] = None


@dataclass
class TcpHangState:
    is_hung: bool
    stuck_send_q: int = 0
    duration_stuck_s: float = 0.0
    reason: Optional[str] = None


class TcpSocketInspector:
    """提取系统 TCP Socket 的 Send-Q 队列与状态。"""

    @staticmethod
    def inspect_pid_sockets(pid: int) -> List[SocketInfo]:
        """通过 ss 命令检查特定 PID 的网络连接。"""
        results: List[SocketInfo] = []
        try:
            cmd = ["ss", "-tnp", f"pid={pid}"]
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
            for line in out.splitlines():
                line = line.strip()
                if not line or line.startswith("State") or line.startswith("Netid"):
                    continue
                parts = line.split()
                if len(parts) >= 5:
                    try:
                        recv_q = int(parts[1])
                        send_q = int(parts[2])
                        local_addr = parts[3]
                        peer_addr = parts[4]
                        results.append(
                            SocketInfo(
                                local_addr=local_addr,
                                peer_addr=peer_addr,
                                send_q=send_q,
                                recv_q=recv_q,
                                pid=pid,
                            )
                        )
                    except (ValueError, IndexError):
                        continue
        except (subprocess.SubprocessError, FileNotFoundError):
            pass
        return results


class TcpHalfOpenWatchdog:
    """TCP 假死看门狗。"""

    def __init__(
        self,
        inspector: Optional[TcpSocketInspector] = None,
        max_send_q_stuck_s: float = 10.0,
    ):
        self.inspector = inspector or TcpSocketInspector()
        self.max_send_q_stuck_s = max_send_q_stuck_s
        self._stuck_since: Optional[float] = None
        self._last_send_q = 0

    def evaluate_sockets(self, sockets: List[SocketInfo], now: Optional[float] = None) -> TcpHangState:
        """评估 Socket 列表是否存在持续堆积不降的假死现象。"""
        current_ts = now or time.monotonic()
        max_send_q = max((s.send_q for s in sockets), default=0)

        if max_send_q > 0:
            if self._stuck_since is None:
                self._stuck_since = current_ts
                self._last_send_q = max_send_q
            duration = current_ts - self._stuck_since
            is_hung = duration >= self.max_send_q_stuck_s

            return TcpHangState(
                is_hung=is_hung,
                stuck_send_q=max_send_q,
                duration_stuck_s=duration,
                reason=(
                    f"Send-Q 持续积压 {max_send_q} bytes 达 {duration:.1f}s 无 ACK (阈值: {self.max_send_q_stuck_s}s)"
                    if is_hung
                    else None
                ),
            )

        # 队列已清空或正常
        self._stuck_since = None
        self._last_send_q = 0
        return TcpHangState(is_hung=False)
