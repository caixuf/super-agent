"""agentcore.baseline_config — Single Source of Truth for Super Agent Runtime Baseline.

Eliminates cross-instance configuration drift between deployments.
Ensures that regardless of which host or profile an instance runs under,
core user experience contracts (streaming edit transport, clean card replies,
disabled delivery guards, and steer input mode) are strictly enforced
and auto-repaired during installation and preflight.
"""
from __future__ import annotations

import copy
import datetime as dt
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


class ConfigDeviation(NamedTuple):
    key_path: str
    expected: Any
    actual: Any
    severity: str  # "critical" | "warning"


# The authoritative Super Agent 2.0 configuration baseline contract.
CORE_RUNTIME_BASELINE: Dict[str, Any] = {
    "streaming": {
        "enabled": True,
        "transport": "edit",
        "buffer_threshold": 120,
        "edit_interval": 0.8,
    },
    "display": {
        "busy_ack_enabled": True,
        "busy_input_mode": "steer",
        "platforms": {
            "feishu": {
                "streaming": True,
                "busy_ack_detail": True,
                "cleanup_progress": True,
                "tool_progress": False,
                "thinking_progress": False,
            }
        },
    },
    "platforms": {
        "feishu": {
            "enabled": True,
            "card_replies": True,
            "exec_approval_text": True,
        }
    },
    "plugins": {
        "entries": {
            "illustrated-delivery": {
                "settings": {
                    "enforce_delivery": False,
                }
            }
        }
    },
}


def _deep_get(mapping: Mapping[str, Any], path: Sequence[str]) -> Tuple[bool, Any]:
    curr: Any = mapping
    for step in path:
        if not isinstance(curr, Mapping) or step not in curr:
            return False, None
        curr = curr[step]
    return True, curr


def _leading_comment_prefix(raw_text: str) -> str:
    """Keep leading blank/comment lines so installer rewrites do not drop them."""
    lines: List[str] = []
    for line in raw_text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            lines.append(line)
            continue
        break
    return "".join(lines)


def _deep_set(target: Dict[str, Any], path: Sequence[str], value: Any) -> None:
    curr = target
    for step in path[:-1]:
        if step not in curr or not isinstance(curr[step], dict):
            curr[step] = {}
        curr = curr[step]
    curr[path[-1]] = value


def _traverse_baseline(
    baseline: Mapping[str, Any], prefix: Tuple[str, ...] = ()
) -> List[Tuple[Tuple[str, ...], Any]]:
    items: List[Tuple[Tuple[str, ...], Any]] = []
    for k, v in baseline.items():
        curr_path = prefix + (k,)
        if isinstance(v, Mapping):
            items.extend(_traverse_baseline(v, curr_path))
        else:
            items.append((curr_path, v))
    return items


def audit_config_dict(
    config: Mapping[str, Any],
    baseline: Mapping[str, Any] = CORE_RUNTIME_BASELINE,
) -> List[ConfigDeviation]:
    """Audit a configuration dictionary against the canonical baseline."""
    deviations: List[ConfigDeviation] = []
    leaves = _traverse_baseline(baseline)

    for path, expected in leaves:
        path_str = ".".join(path)
        exists, actual = _deep_get(config, path)
        if not exists:
            deviations.append(
                ConfigDeviation(
                    key_path=path_str,
                    expected=expected,
                    actual=None,
                    severity="critical",
                )
            )
        elif actual != expected:
            # Special case: truthy string representation matching
            if isinstance(expected, bool) and str(actual).lower() == str(expected).lower():
                continue
            deviations.append(
                ConfigDeviation(
                    key_path=path_str,
                    expected=expected,
                    actual=actual,
                    severity="critical",
                )
            )
    return deviations


def enforce_baseline_config(
    config_path: Path,
    baseline: Mapping[str, Any] = CORE_RUNTIME_BASELINE,
    *,
    backup: bool = True,
) -> Tuple[bool, List[str]]:
    """Idempotently enforce the baseline onto an actual config.yaml file.

    Returns (was_modified, changes_applied).
    """
    config_path = Path(config_path).resolve()
    if not config_path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML required for config enforcement") from exc

    raw_text = config_path.read_text(encoding="utf-8")
    loaded = yaml.safe_load(raw_text) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Invalid YAML content in {config_path}: expected dictionary")

    deviations = audit_config_dict(loaded, baseline)
    if not deviations:
        return False, []

    modified = copy.deepcopy(loaded)
    changes: List[str] = []

    for dev in deviations:
        parts = dev.key_path.split(".")
        _deep_set(modified, parts, dev.expected)
        changes.append(f"{dev.key_path}: {dev.actual!r} -> {dev.expected!r}")

    # Atomic write with backup
    if backup:
        timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        bak_file = config_path.with_name(f"{config_path.name}.bak.{timestamp}")
        shutil.copy2(config_path, bak_file)

    temp_file = config_path.with_suffix(".tmp")
    dumped = yaml.dump(modified, allow_unicode=True, sort_keys=False)
    prefix = _leading_comment_prefix(raw_text)
    with temp_file.open("w", encoding="utf-8") as f:
        f.write(prefix)
        f.write(dumped)
    os.replace(temp_file, config_path)

    return True, changes


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Super Agent 运行时配置基线审计与修复")
    parser.add_argument("--config", "-c", help="目标 config.yaml 路径（默认自动解析当前 profile）")
    parser.add_argument("--check-only", action="store_true", help="仅审计不修改")
    args = parser.parse_args()

    cfg_path = args.config
    if not cfg_path:
        home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        active = Path(home) / "active_profile"
        if active.is_file():
            p_name = active.read_text(encoding="utf-8").strip()
            cfg_path = Path(home) / "profiles" / p_name / "config.yaml"
        else:
            cfg_path = Path(home) / "config.yaml"

    path = Path(cfg_path).resolve()
    print(f"[Super Agent] 正在审计配置基线: {path}")

    import yaml
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    devs = audit_config_dict(data)
    if not devs:
        print("[PASS] 目标配置完全符合 Super Agent 2.0 核心基线契约！")
        return

    print(f"[FAIL] 发现 {len(devs)} 项基线配置偏离:")
    for d in devs:
        print(f"       - {d.key_path}: 当前={d.actual!r}, 期望={d.expected!r}")

    if args.check_only:
        raise SystemExit(1)

    print("[FIX]  执行自动化基线安全对齐...")
    mod, changes = enforce_baseline_config(path)
    if mod:
        print(f"[OK]   已修复 {len(changes)} 项配置，并保留原有私有配置，备份已生成。")
        for ch in changes:
            print(f"       -> {ch}")


if __name__ == "__main__":
    main()
