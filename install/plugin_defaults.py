#!/usr/bin/env python3
"""Install safe, profile-local plugin defaults without rewriting existing entries.

Only absent entries are filled. Existing team identities, scopes, credentials and
explicit opt-outs always belong to the deployer. This is not runtime readiness.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

import enable_plugins as config_text


def _mapping(lines, start, end):
    """Read one block mapping; refuse ambiguous/flow structures before writing."""
    raw = config_text._strip_comment(lines[start].split(":", 1)[1])
    indent = config_text._indent(lines[start])
    children = [i for i in range(start + 1, end)
                if lines[i].strip() and not lines[i].lstrip().startswith("#")]
    if raw not in ("", "{}", "null", "~") or (raw and children):
        raise config_text.Unsupported("non-block plugin mapping; preserve it and configure manually")
    if not children:
        return {}, indent + 2
    child_indent = config_text._indent(lines[children[0]])
    if child_indent <= indent:
        raise config_text.Unsupported("invalid plugin mapping indentation")
    keys = {}
    for i in children:
        if config_text._has_tab_indent(lines[i]):
            raise config_text.Unsupported("tab indentation")
        if config_text._indent(lines[i]) != child_indent:
            continue
        # YAML permits a block sequence at the same indentation as its key.
        if lines[i].lstrip().startswith("- "):
            continue
        match = re.fullmatch(r"\s*([A-Za-z0-9_-]+)\s*:(.*)", lines[i])
        if not match or match[1] in keys:
            raise config_text.Unsupported("ambiguous plugin mapping key")
        keys[match[1]] = i
    return keys, child_indent


def _end(lines, start, outer_end):
    indent = config_text._indent(lines[start])
    return next((i for i in range(start + 1, outer_end)
                 if lines[i].strip() and not lines[i].lstrip().startswith("#")
                 and config_text._indent(lines[i]) <= indent), outer_end)


def _entries(text):
    lines = text.splitlines()
    shape = config_text._shape(lines)
    if shape["kind"] != "mapping":
        raise config_text.Unsupported("plugin enable mapping must be installed first")
    at = config_text._find_child(lines, shape["top"], shape["end"], "entries", shape["child"])
    if at is None:
        return lines, shape, None, {}, shape["child"] + 2
    keys, indent = _mapping(lines, at, _end(lines, at, shape["end"]))
    return lines, shape, at, keys, indent


def eligible_names(text, names):
    """Default installation never reverses an explicit per-profile opt-out."""
    view = config_text.inspect(text)
    if view["structure"] != "mapping":
        return list(names)
    lines, shape, _, entries, _ = _entries(text)
    result = []
    for name in names:
        if name in view["disabled"]:
            continue
        if name in entries:
            start = entries[name]
            end = _end(lines, start, shape["end"])
            keys, _ = _mapping(lines, start, end)
            checks = [keys.get("enabled")]
            if "settings" in keys:
                settings, _ = _mapping(lines, keys["settings"], _end(lines, keys["settings"], end))
                checks.append(settings.get("enabled"))
            if any(i is not None and config_text._strip_comment(lines[i].split(":", 1)[1]).lower()
                   == "false" for i in checks):
                continue
        result.append(name)
    return result


def defaults(home: Path, engine: Path, profile: str):
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", profile):
        raise config_text.Unsupported("profile is not a valid colleague primary_id")
    engine_settings = {"engine_root": str(engine)}
    ledger = str(home / "agentcore-state" / "liveness.db")
    return {
        "illustrated-delivery": {"enabled": True, **engine_settings},
        "longtask-guard": {**engine_settings, "liveness_db": ledger},
        "task-status": {"enabled": True, **engine_settings, "liveness_db": ledger},
        "colleague-catalog": {"enabled": True, **engine_settings, "roles": {"analysis": {
            "display_name": "Analyst", "duty": "Review supplied evidence and report findings.",
            "can_do": ["Summarize source-backed findings."],
            "boundaries": ["Read only; do not change files.",
                           "This description grants no dispatch or file access permissions."],
        }}},
        "colleague-team": {"enabled": True, **engine_settings,
                           "team_id": "team-" + hashlib.sha256(str(home).encode()).hexdigest()[:16],
                           "primary_id": profile,
                           "registry_path": str(home / "agentcore-state" / "colleague-team" / "registry.db")},
    }


def _render(mapping, indent):
    lines = []
    for key, value in mapping.items():
        prefix = " " * indent + key + ":"
        if isinstance(value, dict):
            lines.append(prefix)
            lines.extend(_render(value, indent + 2))
        else:
            # JSON scalar/list literals are YAML-compatible; no escaping ambiguity.
            lines.append(prefix + " " + json.dumps(value, ensure_ascii=False))
    return lines


def merge_defaults(text, values):
    lines, shape, at, entries, indent = _entries(text)
    eligible = set(eligible_names(text, list(values)))
    missing = {name: {"settings": settings} for name, settings in values.items()
               if name not in entries and name in eligible}
    if not missing:
        return text, []
    if at is None:
        position = shape["end"]
        insertion = [" " * shape["child"] + "entries:"] + _render(missing, indent)
    else:
        position = _end(lines, at, shape["end"])
        raw, comment = config_text._split_comment(lines[at])
        lines[at] = " " * shape["child"] + "entries:" + (" " + comment if comment else "")
        insertion = _render(missing, indent)
    lines[position:position] = insertion
    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), list(missing)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--engine-root", type=Path)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--eligible-name", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        text = args.config.read_text(encoding="utf-8") if args.config.exists() else ""
        if args.eligible_name:
            print("\n".join(eligible_names(text, args.eligible_name)))
            return 0
        if args.home is None or args.engine_root is None:
            parser.error("--home and --engine-root are required for defaults")
        home = args.home.resolve()
        values = defaults(home, args.engine_root.resolve(strict=True), args.profile)
        # During dry-run, enable_plugins has not created the mapping on disk.
        if not text.strip() or config_text.inspect(text)["structure"] != "mapping":
            text, _ = config_text.merge(text, [])
        changed, names = merge_defaults(text, values)
        if args.dry_run:
            for name in names:
                print("    [plan] plugins.entries." + name + ".settings defaults")
            return 0
        if names:
            if "colleague-team" in names:
                directory = home / "agentcore-state" / "colleague-team"
                if directory.is_symlink():
                    raise config_text.Unsupported("team registry directory must not be a symlink")
                if directory.exists():
                    info = directory.stat()
                    if info.st_mode & 0o077 or info.st_uid != os.getuid():
                        raise config_text.Unsupported("existing team registry directory is not private")
                else:
                    directory.mkdir(parents=True, mode=0o700)
            config_text._backup(args.config)
            config_text._write_atomic(args.config, changed)
            for name in names:
                print("  [OK] 安装安全默认设置 " + name)
        else:
            print("  [--] 插件默认设置已存在（保留现场 entries 与显式关闭）")
        return 0
    except (OSError, config_text.Unsupported) as error:
        # Never print config contents: malformed shapes may include inline secrets.
        print("plugin_defaults: cannot safely configure defaults (" + type(error).__name__ +
              "); existing settings preserved; configure plugin entries manually", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
