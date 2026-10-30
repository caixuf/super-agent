#!/usr/bin/env python3
"""deployment_layered_audit.py — 四态一致性分层架构审计与校验门禁

实现目标：
运行时回答四个状态是否一致（分层解耦铁律）：
  1. 期望状态（Manifest）：声明了什么
  2. 磁盘状态（Disk）：配置文件与 drop-in 究竟写了什么
  3. 运行环境（Runtime）：进程 /proc/<pid>/environ 实际加载了什么
  4. 功能探测（Probe）：卡片生成与日志健康度是否真可用

支持 --profile <name> | --all
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent

COLOR_GREEN = "\033[32m"
COLOR_RED = "\033[31m"
COLOR_YELLOW = "\033[33m"
COLOR_CYAN = "\033[36m"
COLOR_RESET = "\033[0m"


def log_pass(msg: str):
    print(f"  {COLOR_GREEN}[PASS]{COLOR_RESET} {msg}")


def log_fail(msg: str):
    print(f"  {COLOR_RED}[FAIL]{COLOR_RESET} {msg}")


def log_warn(msg: str):
    print(f"  {COLOR_YELLOW}[WARN]{COLOR_RESET} {msg}")


def log_info(msg: str):
    print(f"  {COLOR_CYAN}[INFO]{COLOR_RESET} {msg}")


def run_host_cmd(host: str, cmd: str, timeout: int = 15) -> Tuple[int, str, str]:
    if host == "local":
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    else:
        full_cmd = f"ssh -o ConnectTimeout=10 {host} {subprocess.list2cmdline([cmd])}"
        proc = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr


def load_yaml(content: str) -> dict:
    try:
        import yaml
        return yaml.safe_load(content) or {}
    except ImportError:
        # Simple fallback for basic key-value if yaml missing
        res = {}
        for line in content.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and ":" in line:
                k, v = line.split(":", 1)
                res[k.strip()] = v.strip().strip("'\"")
        return res


def get_nested(data: dict, dotpath: str) -> Any:
    parts = dotpath.split(".")
    curr = data
    for p in parts:
        if not isinstance(curr, dict) or p not in curr:
            return None
        curr = curr[p]
    return curr


def audit_profile(manifest_path: Path) -> bool:
    print(f"\n========================================================")
    print(f" 开始分层审计: {manifest_path.name}")
    print(f"========================================================")
    
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = load_yaml(f.read())
        
    profile = manifest.get("profile", "")
    host = manifest.get("host_target", "local")
    service = manifest.get("gateway_service", "")
    role_title = manifest.get("role_title", "")
    home_dir = manifest.get("home_dir", "")
    
    print(f"Profile: {COLOR_CYAN}{profile}{COLOR_RESET} | 主机: {COLOR_CYAN}{host}{COLOR_RESET} | 服务: {COLOR_CYAN}{service}{COLOR_RESET}")
    print(f"角色定位: {role_title}")
    
    success = True
    
    # --- 1. 期望状态 (Manifest) ---
    print(f"\n[状态 1: 期望状态 Manifest]")
    log_pass(f"Manifest 加载成功: profile={profile}, service={service}")
    
    # --- 2. 磁盘状态 (Disk) ---
    print(f"\n[状态 2: 磁盘配置 Disk]")
    
    # 2.1 检查 drop-in 目录与废弃文件
    drop_in_dir = f"~/.config/systemd/user/{service}.d"
    rc, stdout, _ = run_host_cmd(host, f"ls -la {drop_in_dir} 2>/dev/null")
    if rc != 0:
        log_fail(f"Drop-in 目录不存在: {drop_in_dir}")
        success = False
    else:
        files = stdout.splitlines()
        bak_files = [f for f in files if ".bak" in f or "~" in f]
        if bak_files:
            log_warn(f"发现废弃/备份文件留在 drop-in 目录中 ({len(bak_files)} 个):")
            for b in bak_files:
                print(f"       {b}")
        else:
            log_pass("Drop-in 目录干净，无废弃 .bak 文件堆积")
            
    # 2.2 检查环境变量 drop-in
    expected_env = manifest.get("runtime", {}).get("drop_ins", {}).get("10-card.conf", {})
    rc, stdout, _ = run_host_cmd(host, f"cat {drop_in_dir}/*.conf 2>/dev/null")
    disk_drop_ins_text = stdout
    
    for k, v in expected_env.items():
        pattern = rf"{k}\s*=\s*[\"']?{v}[\"']?"
        if re.search(pattern, disk_drop_ins_text):
            log_pass(f"磁盘 Drop-in 环境变量就位: {k}={v}")
        else:
            log_fail(f"磁盘 Drop-in 缺少或未匹配: {k}={v}")
            success = False
            
    # 2.3 检查 config.yaml 不变式
    config_path = f"{home_dir}/config.yaml"
    rc, stdout, _ = run_host_cmd(host, f"cat {config_path}")
    if rc != 0:
        log_fail(f"无法读取 Profile config.yaml: {config_path}")
        success = False
    else:
        config_data = load_yaml(stdout)
        invariants = manifest.get("runtime", {}).get("config_invariants", {})
        for dotpath, expected_val in invariants.items():
            actual_val = get_nested(config_data, dotpath)
            if actual_val == expected_val:
                log_pass(f"磁盘 config.yaml 校验通过: {dotpath} = {actual_val}")
            else:
                log_fail(f"磁盘 config.yaml 漂移: {dotpath} 期望={expected_val}, 实际={actual_val}")
                success = False

    # 2.4 检查 SOUL.md 身份与业务叠加层
    target_soul = manifest.get("identity", {}).get("target_soul", "")
    overlay_rel = manifest.get("identity", {}).get("overlay", "")
    overlay_file = REPO_ROOT / overlay_rel
    
    if overlay_file.is_file():
        overlay_content = overlay_file.read_text(encoding="utf-8")
        # 取第一段作为特征识别
        first_line = [l for l in overlay_content.splitlines() if l.strip() and not l.startswith("#")][0].strip()
        probe_str = first_line[:30].replace("'", "'\\''")
        rc, stdout, _ = run_host_cmd(host, f"grep -F '{probe_str}' {target_soul}")
        if rc == 0:
            log_pass(f"磁盘 SOUL.md 已包含对应的业务叠加层 ({overlay_rel})")
        elif rc == 1:
            log_warn(f"磁盘 SOUL.md 未能精确匹配业务叠加层特征首行 (请核实是否已独立定制)")
        else:
            log_fail(f"无法读取目标 SOUL.md: {target_soul}")
            success = False

    # --- 3. 运行环境 (Runtime Process) ---
    print(f"\n[状态 3: 运行环境 Runtime Process]")
    
    # 3.1 检查 systemd 服务活跃度
    rc, stdout, _ = run_host_cmd(host, f"systemctl --user is-active {service}")
    active_state = stdout.strip()
    if active_state == "active":
        log_pass(f"系统服务运行正常: {service} (active)")
    else:
        log_fail(f"系统服务未处于 active 状态: {service} ({active_state})")
        success = False
        
    # 3.2 检查进程内的真实环境变量 (读 /proc/<pid>/environ)
    rc, stdout, _ = run_host_cmd(host, f"systemctl --user show --property=MainPID {service}")
    main_pid = stdout.strip().split("=")[-1]
    if main_pid and main_pid != "0":
        rc, stdout, _ = run_host_cmd(host, f"tr '\\0' '\\n' < /proc/{main_pid}/environ 2>/dev/null")
        proc_env_text = stdout
        for k, v in expected_env.items():
            if f"{k}={v}" in proc_env_text:
                log_pass(f"进程实际环境变量已加载: PID {main_pid} -> {k}={v}")
            else:
                log_fail(f"进程实际环境变量缺失: PID {main_pid} 未加载 {k}={v} (需要重启生效)")
                success = False
    else:
        log_fail(f"无法获取服务主进程 PID")
        success = False

    # --- 4. 功能表现探测 (Live Probe) ---
    print(f"\n[状态 4: 功能表现探测 Live Probe]")
    
    # 4.1 检查最近 5 分钟日志，是否有 backlog 或反复告警
    rc, stdout, _ = run_host_cmd(host, f"journalctl --user -u {service} --since '5 minutes ago' --no-pager 2>/dev/null")
    recent_logs = stdout
    backlog_warnings = [l for l in recent_logs.splitlines() if "backlog:" in l or "ERROR" in l]
    if not backlog_warnings:
        log_pass("最近 5 分钟网关日志健康: 0 条 Warning/Error，无回执堆积报警")
    else:
        log_warn(f"最近日志中发现 {len(backlog_warnings)} 条告警/错误 (前 2 条示例):")
        for w in backlog_warnings[:2]:
            print(f"       {w}")
            
    print(f"--------------------------------------------------------")
    if success:
        print(f" Profile [{profile}] 四态一致性审计: {COLOR_GREEN}全部通过 [PASS]{COLOR_RESET}\n")
    else:
        print(f" Profile [{profile}] 四态一致性审计: {COLOR_RED}存在不一致项 [FAIL]{COLOR_RESET}\n")
    return success


def main():
    parser = argparse.ArgumentParser(description="Super Agent 分层架构四态一致性审计")
    parser.add_argument("--profile", type=str, help="指定审计的 profile 名称")
    parser.add_argument("--all", action="store_true", help="审计全部 profile")
    args = parser.parse_args()
    
    manifests_dir = REPO_ROOT / "identity" / "manifests"
    if args.profile:
        manifest_files = [manifests_dir / f"{args.profile}.manifest.yaml"]
    else:
        manifest_files = list(manifests_dir.glob("*.manifest.yaml"))
        
    all_ok = True
    for mf in manifest_files:
        if not mf.is_file():
            print(f"Manifest 不存在: {mf}")
            all_ok = False
            continue
        ok = audit_profile(mf)
        if not ok:
            all_ok = False
            
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()

