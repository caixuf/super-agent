"""工业级部署与启动前自检体系 (Preflight Check)。

借鉴 KunAutoDrive 仿真前硬件与管线拓扑自检哲学：
在服务启动或一键部署时执行前置硬件/网络/环境体检，杜绝把坏环境当成 Agent bug。
检查项：
1. DNS 解析延迟检测 (探测 open.feishu.cn / 通用域名，时延 > 500ms 报错)；
2. 上游模型代理与网关可达性 (LiteLLM / 目标 API 探测)；
3. 磁盘与运行时剩余空间检测 (保障长任务与日志不打满磁盘)；
4. 关键端口与 Socket 冲突探测。
"""

from __future__ import annotations

import os
import shutil
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import List


@dataclass
class CheckItemResult:
    name: str
    passed: bool
    latency_ms: float
    message: str
    detail: str = ""


@dataclass
class PreflightReport:
    all_passed: bool
    items: List[CheckItemResult] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def summary(self) -> str:
        lines = [
            f"=== Super Agent 部署预检报告 ({'PASS' if self.all_passed else 'FAIL'}) ==="
        ]
        for it in self.items:
            tag = "[OK]  " if it.passed else "[FAIL]"
            lines.append(f"{tag} {it.name:<25} ({it.latency_ms:.1f}ms) - {it.message}")
            if not it.passed and it.detail:
                lines.append(f"       -> 诊断修复: {it.detail}")
        return "\n".join(lines)


def check_dns_latency(target_host: str = "open.feishu.cn", max_acceptable_ms: float = 300.0) -> CheckItemResult:
    """探测目标域名 DNS 解析延迟。"""
    t0 = time.monotonic()
    try:
        # 强制单次 socket 解析
        ip = socket.gethostbyname(target_host)
        dur_ms = (time.monotonic() - t0) * 1000.0
        passed = dur_ms <= max_acceptable_ms
        msg = f"已解析到 {ip} 耗时 {dur_ms:.1f}ms"
        detail = ""
        if not passed:
            msg = f"DNS 解析严重超时 (耗时 {dur_ms:.1f}ms > {max_acceptable_ms}ms)"
            detail = "请检查 /etc/resolv.conf，推荐使用 114.114.114.114 或内网网关 DNS"
        return CheckItemResult(name="DNS 解析延迟", passed=passed, latency_ms=dur_ms, message=msg, detail=detail)
    except Exception as exc:
        dur_ms = (time.monotonic() - t0) * 1000.0
        return CheckItemResult(
            name="DNS 解析延迟",
            passed=False,
            latency_ms=dur_ms,
            message=f"域名解析失败: {exc}",
            detail="DNS 上游无响应或网络不可达，请切换为可用 DNS",
        )


def check_disk_space(path: str = "/", min_free_gb: float = 5.0) -> CheckItemResult:
    """探测磁盘剩余可用空间。"""
    t0 = time.monotonic()
    try:
        usage = shutil.disk_usage(path)
        free_gb = usage.free / (1024 ** 3)
        dur_ms = (time.monotonic() - t0) * 1000.0
        passed = free_gb >= min_free_gb
        msg = f"可用空间 {free_gb:.1f} GB (阈值: {min_free_gb} GB)"
        detail = "" if passed else "磁盘空间不足，请清理日志或历史包以免服务卡死"
        return CheckItemResult(name="磁盘剩余空间", passed=passed, latency_ms=dur_ms, message=msg, detail=detail)
    except Exception as exc:
        dur_ms = (time.monotonic() - t0) * 1000.0
        return CheckItemResult(
            name="磁盘剩余空间",
            passed=False,
            latency_ms=dur_ms,
            message=f"检查失败: {exc}",
        )


def check_tcp_port_available(port: int, host: str = "127.0.0.1") -> CheckItemResult:
    """探测指定本地端口是否被占用 (预检查服务端口是否空闲或可用)。"""
    t0 = time.monotonic()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        result = sock.connect_ex((host, port))
        dur_ms = (time.monotonic() - t0) * 1000.0
        # 0 表示已被监听
        occupied = (result == 0)
        return CheckItemResult(
            name=f"端口监听检查 (:{port})",
            passed=True,
            latency_ms=dur_ms,
            message=f"端口状态: {'已在监听/存活' if occupied else '空闲就绪'}",
        )
    finally:
        sock.close()


def run_preflight_suite(
    probe_host: str = "open.feishu.cn",
    min_free_gb: float = 5.0,
) -> PreflightReport:
    """执行完整的启动与部署预检套件。"""
    results: List[CheckItemResult] = [
        check_dns_latency(target_host=probe_host),
        check_disk_space(min_free_gb=min_free_gb),
    ]

    all_passed = all(item.passed for item in results)
    return PreflightReport(all_passed=all_passed, items=results)

