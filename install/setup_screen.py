#!/usr/bin/env python3
"""Host diagnostics, dependency setup, and configuration CLI for Bot Desktop.

Manages the isolated virtual X11/VNC desktop (Bot Desktop) integrated with
Hermes computer_use and Super Agent human-takeover lease observation.

Usage:
  python3 -m install.setup_screen doctor [--hermes-home PATH] [--min-free-mb 1536] [--json]
  python3 -m install.setup_screen install-deps [--distro auto|debian|fedora|arch] [--apply]
  python3 -m install.setup_screen configure --hermes-home PATH [--geometry 1440x900] [--auto-start] [--placement auto|terminal|gateway] [--idle-stop-minutes 30] [--min-free-mb 1536] [--apply]
  python3 -m install.setup_screen status [--hermes-home PATH] [--hermes-bin PATH]
  python3 -m install.setup_screen start  [--hermes-home PATH] [--hermes-bin PATH]
  python3 -m install.setup_screen stop   [--hermes-home PATH] [--hermes-bin PATH]
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agentcore.screen_lease import read_bot_desktop_lease

REQUIRED_BINARIES: Tuple[str, ...] = (
    "Xvnc",
    "xfwm4",
    "xfce4-panel",
    "xfdesktop",
    "xfsettingsd",
    "dbus-run-session",
    "xauth",
    "xdpyinfo",
    "setxkbmap",
    "xprop",
)

DISTRO_INSTALL_COMMANDS: Dict[str, List[str]] = {
    "debian": [
        "apt-get",
        "install",
        "-y",
        "--no-install-recommends",
        "tigervnc-standalone-server",
        "xfce4-panel",
        "xfwm4",
        "xfdesktop4",
        "xfce4-settings",
        "xfce4-terminal",
        "dbus-x11",
        "x11-xserver-utils",
        "x11-utils",
        "x11-xkb-utils",
        "xauth",
        "fonts-dejavu-core",
    ],
    "fedora": [
        "dnf",
        "install",
        "-y",
        "tigervnc-x11-server",
        "xfce4-panel",
        "xfwm4",
        "xfdesktop",
        "xfce4-settings",
        "xfce4-terminal",
        "dbus-daemon",
        "xsetroot",
        "xset",
        "xdpyinfo",
        "xprop",
        "xorg-x11-xauth",
        "setxkbmap",
        "dejavu-sans-fonts",
    ],
    "arch": [
        "pacman",
        "-S",
        "--needed",
        "--noconfirm",
        "tigervnc",
        "xfce4-panel",
        "xfwm4",
        "xfdesktop",
        "xfce4-settings",
        "xfce4-terminal",
        "dbus",
        "xorg-xsetroot",
        "xorg-xset",
        "xorg-xdpyinfo",
        "xorg-xprop",
        "xorg-xauth",
        "xorg-setxkbmap",
        "ttf-dejavu",
    ],
}

_GEOMETRY_RE = re.compile(r"^\d{3,5}x\d{3,5}\Z")
_VALID_PLACEMENTS = ("auto", "terminal", "gateway")


def resolve_hermes_home(
    explicit: Optional[Path | str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Path:
    """Resolve HERMES_HOME (--hermes-home > $HERMES_HOME > ~/.hermes)."""
    if explicit is not None and str(explicit).strip():
        return Path(str(explicit).strip()).expanduser().resolve()
    source_env = os.environ if env is None else env
    env_val = (source_env.get("HERMES_HOME") or "").strip()
    if env_val:
        return Path(env_val).expanduser().resolve()
    return (Path.home() / ".hermes").resolve()


def resolve_hermes_bin(
    hermes_home: Path,
    explicit: Optional[Path | str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Optional[Path]:
    """Locate the hermes CLI binary according to deterministic precedence."""
    if explicit is not None and str(explicit).strip():
        p = Path(str(explicit).strip()).expanduser()
        return p

    source_env = os.environ if env is None else env
    env_bin = (source_env.get("HERMES_BIN") or "").strip()
    if env_bin:
        return Path(env_bin).expanduser()

    candidates: List[Path] = []
    if hermes_home.parent.name == "profiles":
        candidates.append(
            Path.home() / "super_agent" / hermes_home.name / "hermes-current" / ".hermes" / "bin" / "hermes"
        )
    candidates.extend(
        [
            (hermes_home.parent / ".hermes" / "bin" / "hermes"),
            (Path.home() / ".local" / "bin" / "hermes"),
        ]
    )
    for cand in candidates:
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand

    which_hit = shutil.which("hermes")
    if which_hit:
        return Path(which_hit)

    return None


def detect_distro(os_release_path: Path = Path("/etc/os-release")) -> str:
    """Detect normalized host package family ('debian', 'fedora', or 'arch')."""
    try:
        text = os_release_path.read_text(encoding="utf-8")
    except OSError:
        return "debian"

    fields: Dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        fields[k.strip().upper()] = v.strip().strip('"').strip("'").lower()

    tokens = set()
    for key in ("ID", "ID_LIKE"):
        tokens.update(fields.get(key, "").split())

    if tokens & {"debian", "ubuntu", "linuxmint", "pop", "kali", "raspbian"}:
        return "debian"
    if tokens & {"fedora", "rhel", "centos", "rocky", "almalinux"}:
        return "fedora"
    if tokens & {"arch", "manjaro", "endeavouros"}:
        return "arch"
    return "debian"


def build_install_deps_command(distro: str) -> List[str]:
    """Return minimal Bot Desktop host package installation argv for *distro*."""
    if distro not in DISTRO_INSTALL_COMMANDS:
        raise ValueError(f"Unsupported distro '{distro}': expected one of {sorted(DISTRO_INSTALL_COMMANDS)}")
    return list(DISTRO_INSTALL_COMMANDS[distro])


def read_mem_available_mb(meminfo_path: Path = Path("/proc/meminfo")) -> Optional[int]:
    """Parse MemAvailable (or MemFree fallback) in MiB from /proc/meminfo."""
    try:
        text = meminfo_path.read_text(encoding="utf-8")
    except OSError:
        return None

    mem_avail_kb: Optional[int] = None
    mem_free_kb: Optional[int] = None
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                mem_avail_kb = int(parts[1])
                break
        elif line.startswith("MemFree:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                mem_free_kb = int(parts[1])

    chosen_kb = mem_avail_kb if mem_avail_kb is not None else mem_free_kb
    if chosen_kb is None:
        return None
    return chosen_kb // 1024


def inspect_host_screen_readiness(
    hermes_home: Path,
    min_free_mb: int = 1536,
    *,
    meminfo_path: Path = Path("/proc/meminfo"),
    os_release_path: Path = Path("/etc/os-release"),
) -> Dict[str, Any]:
    """Evaluate required host binaries, available RAM, and Bot Desktop lease."""
    binaries: Dict[str, Optional[str]] = {}
    missing: List[str] = []
    for binary in REQUIRED_BINARIES:
        resolved = shutil.which(binary)
        binaries[binary] = resolved
        if not resolved:
            missing.append(binary)

    avail_mb = read_mem_available_mb(meminfo_path)
    mem_ok = avail_mb is not None and avail_mb >= min_free_mb

    lease = read_bot_desktop_lease(hermes_home).to_public_dict()
    distro = detect_distro(os_release_path)
    install_argv = build_install_deps_command(distro)
    recommended_cmd = "sudo " + shlex.join(install_argv)

    all_ok = (len(missing) == 0) and mem_ok
    return {
        "ok": all_ok,
        "hermes_home": str(hermes_home),
        "distro": distro,
        "binaries": binaries,
        "missing_binaries": missing,
        "memory": {
            "available_mb": avail_mb,
            "min_free_mb": min_free_mb,
            "ok": mem_ok,
        },
        "lease": lease,
        "recommended_install_command": recommended_cmd if missing else None,
    }


def cmd_doctor(
    hermes_home: Path,
    min_free_mb: int = 1536,
    as_json: bool = False,
    *,
    meminfo_path: Path = Path("/proc/meminfo"),
    os_release_path: Path = Path("/etc/os-release"),
) -> int:
    report = inspect_host_screen_readiness(
        hermes_home=hermes_home,
        min_free_mb=min_free_mb,
        meminfo_path=meminfo_path,
        os_release_path=os_release_path,
    )
    if as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["ok"] else 1

    print("=== Super Agent Bot Desktop Doctor ===")
    print(f"Hermes Home : {report['hermes_home']}")
    print(f"Host Distro : {report['distro']}")

    print("\nHost Binaries:")
    for binary in REQUIRED_BINARIES:
        loc = report["binaries"][binary]
        status_tag = "[OK]" if loc else "[MISSING]"
        detail = loc or "not found in PATH"
        print(f"  {status_tag:9s} {binary:16s} -> {detail}")

    mem = report["memory"]
    mem_tag = "[OK]" if mem["ok"] else "[FAIL]"
    avail_str = f"{mem['available_mb']} MiB" if mem["available_mb"] is not None else "unknown"
    print(f"\nMemory Check:\n  {mem_tag:9s} MemAvailable={avail_str} (required >= {mem['min_free_mb']} MiB)")

    lease = report["lease"]
    print("\nScreen Lease Status:")
    print(f"  Holder         : {lease['holder']}")
    print(f"  Human Takeover : {lease['is_human_takeover']}")
    print(f"  Lease File     : {'present' if lease['lease_exists'] else 'absent'}")
    if lease.get("viewer_hash"):
        print(f"  Viewer Hash    : {lease['viewer_hash']}")
    if lease.get("reason"):
        print(f"  Reason         : {lease['reason']}")
    if lease.get("fail_closed"):
        print("  Fail-Closed    : True")

    if report["missing_binaries"]:
        print("\nMissing Dependencies Detected:")
        print(f"  Run: {report['recommended_install_command']}")
        print("  Or : ./install/superagent.sh screen install-deps --apply")

    print(f"\nResult: {'PASS' if report['ok'] else 'FAIL'}")
    return 0 if report["ok"] else 1


def cmd_install_deps(
    distro_arg: str = "auto",
    apply: bool = False,
    *,
    os_release_path: Path = Path("/etc/os-release"),
) -> int:
    distro = detect_distro(os_release_path) if distro_arg == "auto" else distro_arg
    cmd = build_install_deps_command(distro)
    cmd_str = shlex.join(cmd)

    print("=== Bot Desktop Minimal Host Dependencies ===")
    print(f"Target Distro : {distro}")
    print("Note          : Minimal XFCE components only (excludes full xfce4 metapackage to avoid screensaver/polkit lockouts)")
    print(f"Command       : sudo {cmd_str}")

    if not apply:
        print("\n[DRY-RUN] Re-run with --apply to install host packages.")
        return 0

    is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    if is_root:
        exec_argv = cmd
    else:
        if not shutil.which("sudo"):
            print(
                f"[ERROR] 'sudo' not found and current user is non-root. Run manually as root:\n  {cmd_str}",
                file=sys.stderr,
            )
            return 1
        sudo_check = subprocess.run(["sudo", "-n", "true"], capture_output=True, text=True)
        if sudo_check.returncode != 0:
            print(
                f"[ERROR] Passwordless sudo is unavailable. Run manually in an interactive terminal:\n  sudo {cmd_str}",
                file=sys.stderr,
            )
            return 1
        exec_argv = ["sudo", "-n", *cmd]

    res = subprocess.run(exec_argv)
    if res.returncode == 0:
        print("[OK] Bot Desktop host dependencies installed.")
    return int(res.returncode)


def _load_yaml_or_json(text: str) -> Dict[str, Any]:
    if not text.strip():
        return {}
    try:
        import yaml  # type: ignore
        loaded = yaml.safe_load(text)
    except ImportError:
        loaded = json.loads(text)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"config.yaml root must be a mapping, got {type(loaded).__name__}")
    return dict(loaded)


def _dump_yaml_or_json(data: Dict[str, Any]) -> str:
    try:
        import yaml  # type: ignore
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    except ImportError:
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def merge_screen_config(
    existing: Mapping[str, Any],
    *,
    geometry: str = "1440x900",
    auto_start: bool = False,
    placement: str = "auto",
    idle_stop_minutes: int = 30,
    min_free_mb: int = 1536,
) -> Dict[str, Any]:
    """Merge Bot Desktop and computer_use settings into a Hermes config dictionary."""
    if not _GEOMETRY_RE.fullmatch(geometry):
        raise ValueError(f"Invalid geometry '{geometry}': expected <width>x<height> (e.g. 1440x900)")
    if placement not in _VALID_PLACEMENTS:
        raise ValueError(f"Invalid placement '{placement}': expected one of {_VALID_PLACEMENTS}")
    if idle_stop_minutes < 0:
        raise ValueError(f"idle_stop_minutes must be >= 0, got {idle_stop_minutes}")
    if min_free_mb < 0:
        raise ValueError(f"min_free_mb must be >= 0, got {min_free_mb}")

    merged: Dict[str, Any] = dict(existing)

    # When platform_toolsets is omitted in Hermes config.yaml, all default
    # hermes-* platform bundles already include computer_use via _HERMES_CORE_TOOLS.
    # Synthesizing a partial platform_toolsets mapping would replace _HERMES_CORE_TOOLS
    # with ONLY computer_use! Therefore only append computer_use to existing explicit
    # toolset lists that lack both computer_use and a hermes-* bundle.
    raw_pt = merged.get("platform_toolsets")
    if isinstance(raw_pt, list):
        pt_list = list(raw_pt)
        if not any(isinstance(t, str) and (t == "computer_use" or t.startswith("hermes-")) for t in pt_list):
            pt_list.append("computer_use")
        merged["platform_toolsets"] = pt_list
    elif isinstance(raw_pt, dict):
        pt_dict: Dict[str, Any] = {}
        for platform_key, cur in raw_pt.items():
            if isinstance(cur, list):
                cur_list = list(cur)
                if not any(isinstance(t, str) and (t == "computer_use" or t.startswith("hermes-")) for t in cur_list):
                    cur_list.append("computer_use")
                pt_dict[platform_key] = cur_list
            else:
                pt_dict[platform_key] = cur
        merged["platform_toolsets"] = pt_dict

    bot_desktop = dict(merged["bot_desktop"]) if isinstance(merged.get("bot_desktop"), dict) else {}
    bot_desktop.update(
        {
            "geometry": geometry,
            "auto_start": bool(auto_start),
            "min_free_memory_mb": int(min_free_mb),
            "idle_stop_minutes": int(idle_stop_minutes),
            "placement": placement,
        }
    )
    merged["bot_desktop"] = bot_desktop

    computer_use = dict(merged["computer_use"]) if isinstance(merged.get("computer_use"), dict) else {}
    computer_use.setdefault("cua_telemetry", False)
    computer_use["autostart"] = bool(auto_start)
    computer_use.setdefault("max_image_dimension", 1456)
    computer_use.setdefault("capture_after_mode", "som")
    merged["computer_use"] = computer_use

    return merged


def cmd_configure(
    hermes_home: Path,
    *,
    geometry: str = "1440x900",
    auto_start: bool = False,
    placement: str = "auto",
    idle_stop_minutes: int = 30,
    min_free_mb: int = 1536,
    apply: bool = False,
) -> int:
    config_path = hermes_home / "config.yaml"
    before_text = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
    existing_data = _load_yaml_or_json(before_text)

    updated_data = merge_screen_config(
        existing_data,
        geometry=geometry,
        auto_start=auto_start,
        placement=placement,
        idle_stop_minutes=idle_stop_minutes,
        min_free_mb=min_free_mb,
    )
    after_text = _dump_yaml_or_json(updated_data)

    diff_lines = list(
        difflib.unified_diff(
            before_text.splitlines(keepends=True),
            after_text.splitlines(keepends=True),
            fromfile=f"{config_path} (current)",
            tofile=f"{config_path} (planned)",
        )
    )

    print(f"=== Bot Desktop Config Target: {config_path} ===")
    if diff_lines:
        print("".join(diff_lines), end="" if diff_lines[-1].endswith("\n") else "\n")
    else:
        print("(No changes required; configuration already matches requested settings.)")

    if not apply:
        print("\n[DRY-RUN] Re-run with --apply to write config.yaml.")
        return 0

    config_path.parent.mkdir(parents=True, exist_ok=True)
    mode = (config_path.stat().st_mode & 0o777) if config_path.exists() else 0o600
    fd, tmp_name = tempfile.mkstemp(prefix=".screen-config-", dir=str(config_path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(after_text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, config_path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)

    print(f"[OK] Wrote Bot Desktop configuration to {config_path}")
    return 0


def cmd_screen_control(
    action: str,
    hermes_home: Path,
    hermes_bin_arg: Optional[Path] = None,
) -> int:
    if action == "status":
        lease = read_bot_desktop_lease(hermes_home).to_public_dict()
        print("=== Bot Desktop Lease Observation ===")
        print(f"Hermes Home    : {hermes_home}")
        print(f"Lease Holder   : {lease['holder']}")
        print(f"Human Takeover : {lease['is_human_takeover']}")
        print(f"Lease Exists   : {lease['lease_exists']}")
        if lease.get("viewer_hash"):
            print(f"Viewer Hash    : {lease['viewer_hash']}")
        if lease.get("reason"):
            print(f"Reason         : {lease['reason']}")
        if lease.get("fail_closed"):
            print("Fail-Closed    : True")
        print("")

    hermes_bin = resolve_hermes_bin(hermes_home, explicit=hermes_bin_arg)
    if hermes_bin is None:
        print(
            "[ERROR] Could not locate 'hermes' executable. Pass --hermes-bin PATH or set HERMES_BIN.",
            file=sys.stderr,
        )
        return 1

    child_env = dict(os.environ)
    child_env["HERMES_HOME"] = str(hermes_home)
    proc = subprocess.run(
        [str(hermes_bin), "computer-use", "screen", action],
        env=child_env,
    )
    return int(proc.returncode)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Super Agent Bot Desktop host diagnostics and lifecycle CLI"
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_doc = sub.add_parser("doctor", help="Check host VNC/XFCE binaries, available memory, and screen lease")
    p_doc.add_argument("--hermes-home", type=Path, default=None, help="Target HERMES_HOME directory")
    p_doc.add_argument("--min-free-mb", type=int, default=1536, help="Minimum required MemAvailable in MiB")
    p_doc.add_argument("--json", action="store_true", help="Emit machine-readable JSON report")

    p_deps = sub.add_parser("install-deps", help="Install minimal Xvnc and XFCE packages for Bot Desktop")
    p_deps.add_argument(
        "--distro",
        choices=("auto", "debian", "fedora", "arch"),
        default="auto",
        help="Host Linux distribution family (default: auto)",
    )
    p_deps.add_argument("--apply", action="store_true", help="Execute package manager installation")

    p_cfg = sub.add_parser("configure", help="Merge bot_desktop and computer_use settings into config.yaml")
    p_cfg.add_argument("--hermes-home", type=Path, required=True, help="Target HERMES_HOME directory")
    p_cfg.add_argument("--geometry", default="1440x900", help="Virtual desktop geometry (default: 1440x900)")
    p_cfg.add_argument("--auto-start", action="store_true", help="Auto-start Bot Desktop with session/gateway")
    p_cfg.add_argument(
        "--placement",
        choices=_VALID_PLACEMENTS,
        default="auto",
        help="Bot Desktop lifecycle placement (default: auto)",
    )
    p_cfg.add_argument(
        "--idle-stop-minutes",
        type=int,
        default=30,
        help="Minutes of inactivity before stopping Bot Desktop (default: 30)",
    )
    p_cfg.add_argument(
        "--min-free-mb",
        type=int,
        default=1536,
        help="Minimum free host RAM in MiB required before start (default: 1536)",
    )
    p_cfg.add_argument("--apply", action="store_true", help="Write changes to <hermes-home>/config.yaml")

    for act, desc in (
        ("status", "Inspect sanitized takeover lease and Bot Desktop runtime status"),
        ("start", "Start Bot Desktop via Hermes computer-use screen"),
        ("stop", "Stop Bot Desktop via Hermes computer-use screen"),
    ):
        p_act = sub.add_parser(act, help=desc)
        p_act.add_argument("--hermes-home", type=Path, default=None, help="Target HERMES_HOME directory")
        p_act.add_argument("--hermes-bin", type=Path, default=None, help="Explicit path to hermes executable")

    p_web = sub.add_parser("web", help="Serve or install the browser-based Bot Desktop Web Portal")
    p_web.add_argument("--hermes-home", type=Path, default=None, help="Default HERMES_HOME directory")
    p_web.add_argument("--hermes-bin", type=Path, default=None, help="Explicit path to hermes executable")
    p_web.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    p_web.add_argument("--port", type=int, default=9120, help="Bind port (default: 9120)")
    p_web.add_argument(
        "--install-service",
        action="store_true",
        help="Render/install superagent-screen-portal.service instead of running in foreground",
    )
    p_web.add_argument(
        "--unit-dir",
        type=Path,
        default=Path.home() / ".config" / "systemd" / "user",
        help="Systemd user unit directory",
    )
    p_web.add_argument("--apply", action="store_true", help="Write and start systemd service when --install-service is set")

    return parser


def cmd_web(
    hermes_home: Path,
    *,
    hermes_bin: Optional[Path] = None,
    host: str = "127.0.0.1",
    port: int = 9120,
    install_service: bool = False,
    unit_dir: Path = Path.home() / ".config" / "systemd" / "user",
    apply: bool = False,
) -> int:
    from install.screen_web_portal import render_portal_service, serve_portal
    import asyncio

    if install_service:
        unit_name = "superagent-screen-portal.service"
        unit_content = render_portal_service(hermes_home=hermes_home, host=host, port=port)
        dest = unit_dir / unit_name
        print(f"=== Bot Desktop Web Portal Service ({dest}) ===")
        print(unit_content)
        if not apply:
            print("[DRY-RUN] Re-run with --apply to install and start service.")
            return 0
        unit_dir.mkdir(parents=True, exist_ok=True)
        dest.write_text(unit_content, encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "--user", "enable", "--now", unit_name], check=False)
        print(f"[OK] Installed and started {unit_name} -> http://{host}:{port}")
        return 0

    try:
        asyncio.run(serve_portal(hermes_home=hermes_home, host=host, port=port, hermes_bin=hermes_bin))
    except KeyboardInterrupt:
        return 0
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "doctor":
        hermes_home = resolve_hermes_home(args.hermes_home)
        return cmd_doctor(hermes_home, min_free_mb=args.min_free_mb, as_json=args.json)
    if args.subcommand == "install-deps":
        return cmd_install_deps(distro_arg=args.distro, apply=args.apply)
    if args.subcommand == "configure":
        hermes_home = resolve_hermes_home(args.hermes_home)
        try:
            return cmd_configure(
                hermes_home,
                geometry=args.geometry,
                auto_start=args.auto_start,
                placement=args.placement,
                idle_stop_minutes=args.idle_stop_minutes,
                min_free_mb=args.min_free_mb,
                apply=args.apply,
            )
        except ValueError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 2
    if args.subcommand in ("status", "start", "stop"):
        hermes_home = resolve_hermes_home(args.hermes_home)
        return cmd_screen_control(args.subcommand, hermes_home, hermes_bin_arg=args.hermes_bin)
    if args.subcommand == "web":
        hermes_home = resolve_hermes_home(args.hermes_home)
        return cmd_web(
            hermes_home,
            hermes_bin=args.hermes_bin,
            host=args.host,
            port=args.port,
            install_service=args.install_service,
            unit_dir=args.unit_dir,
            apply=args.apply,
        )
    return 2


if __name__ == "__main__":
    sys.exit(main())
