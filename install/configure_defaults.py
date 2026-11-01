#!/usr/bin/env python3
"""Add portable plugin defaults without replacing administrator configuration."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

try:
    import yaml
except ImportError:
    sys.exit("配置默认能力需要 PyYAML（python3-yaml）；请安装后重跑安装器。")


def load(text):
    node = yaml.compose(text)
    if not isinstance(node, yaml.MappingNode):
        raise ValueError("config.yaml must be a YAML mapping")
    def check(n):
        if isinstance(n, yaml.MappingNode):
            seen = set()
            for k, v in n.value:
                if not isinstance(k, yaml.ScalarNode) or k.value in seen:
                    raise ValueError("duplicate or non-scalar YAML mapping key")
                seen.add(k.value)
                check(v)
        elif isinstance(n, yaml.SequenceNode):
            for v in n.value:
                check(v)
    check(node)
    return node


def add_missing(text, keys, value):
    root = load(text)
    node = root
    for index, key in enumerate(keys):
        if not isinstance(node, yaml.MappingNode) or node.flow_style:
            raise ValueError("cannot safely extend non-block mapping: " + ".".join(keys[:index]))
        found = next((v for k, v in node.value if k.value == key), None)
        if found is not None:
            if index == len(keys) - 1:
                return text
            node = found
            continue
        # Insert a whole missing subtree at the end of the block. JSON strings
        # are valid YAML scalars, including paths with spaces or punctuation.
        indent = 0 if node is root else node.start_mark.column
        # A mapping value's first key starts at its actual indentation.
        if node.value:
            indent = node.value[0][0].start_mark.column
        elif node is not root:
            raise ValueError("cannot safely extend empty mapping: " + ".".join(keys[:index]))
        lines = []
        for offset, part in enumerate(keys[index:]):
            prefix = " " * (indent + 2 * offset) + part + ":"
            if index + offset == len(keys) - 1:
                prefix += " " + json.dumps(value, ensure_ascii=False)
            lines.append(prefix + "\n")
        pos = node.end_mark.index
        line_start = text.rfind("\n", 0, pos) + 1
        if not text[line_start:pos].strip():
            pos = line_start
        # YAML end marks can be immediately after the last scalar at EOF.
        before = text[:pos]
        if before and not before.endswith("\n"):
            before += "\n"
        result = before + "".join(lines) + text[pos:]
        load(result)
        return result
    return text



def opted_out(data, plugin):
    plugins = data.get("plugins") or {}
    entry = (plugins.get("entries") or {}).get(plugin) or {}
    settings = entry.get("settings") or {}
    return (plugin in (plugins.get("disabled") or [])
            or entry.get("enabled") is False or settings.get("enabled") is False)


def ensure_toolset(text, plugin, toolset):
    """Add an enabled plugin toolset to saved platform selections only."""
    data = yaml.safe_load(text)
    settings = (((data.get("plugins") or {}).get("entries") or {}).get(plugin) or {}).get("settings") or {}
    if opted_out(data, plugin) or settings.get("enabled") is not True:
        return text
    root = load(text)
    platform = next((v for k, v in root.value if k.value == "platform_toolsets"), None)
    if platform is None:
        return text
    if not isinstance(platform, yaml.MappingNode):
        raise ValueError("platform_toolsets must be a mapping")
    edits = []
    for key, seq in platform.value:
        if not isinstance(seq, yaml.SequenceNode):
            raise ValueError("unsupported saved platform toolsets for " + key.value)
        if any(not isinstance(item, yaml.ScalarNode) for item in seq.value):
            raise ValueError("unsupported platform toolset entry for " + key.value)
        if any(item.value == toolset for item in seq.value):
            continue
        if seq.flow_style:
            pos = seq.end_mark.index - 1
            edits.append((pos, (", " if seq.value else "") + toolset))
        else:
            pos = seq.end_mark.index
            start = text.rfind("\n", 0, pos) + 1
            if not text[start:pos].strip():
                pos = start
            prefix = "" if pos == 0 or text[pos - 1] == "\n" else "\n"
            edits.append((pos, prefix + " " * seq.start_mark.column + "- " + toolset + "\n"))
    for pos, insertion in sorted(edits, reverse=True):
        text = text[:pos] + insertion + text[pos:]
    load(text)
    return text

def main():
    if len(sys.argv) not in (3, 4):
        raise ValueError("usage: configure_defaults.py CONFIG DEPLOY [ANALYSIS_ROOT]")
    config, deploy = map(Path, sys.argv[1:3])
    deploy = deploy.resolve()
    if not (deploy / "agentcore" / "liveness.py").is_file():
        raise ValueError("installed engine missing")
    original = config.read_text(encoding="utf-8")
    db = str(config.parent.resolve() / "agentcore-state" / "liveness.db")
    defaults = {
        "task-status": {"enabled": True, "engine_root": str(deploy), "liveness_db": db},
        "task-progress": {"enabled": True, "engine_root": str(deploy), "liveness_db": db},
        "longtask-guard": {"engine_root": str(deploy), "liveness_db": db},
        "illustrated-delivery": {"enabled": True, "engine_root": str(deploy)},
    }
    if len(sys.argv) == 4:
        source = Path(sys.argv[3]).expanduser()
        if not source.is_absolute() or not source.is_dir():
            raise ValueError("analysis root must be an existing absolute directory")
        existing = yaml.safe_load(original) or {}
        entry = (((existing.get("plugins") or {}).get("entries") or {}).get("durable-dispatch") or {}).get("settings") or {}
        if opted_out(existing, "durable-dispatch"):
            raise ValueError("durable dispatch is explicitly disabled")
        if "allowed_roots" in entry:
            roots = entry["allowed_roots"]
            if not isinstance(roots, list) or str(source.resolve()) not in roots:
                raise ValueError("analysis root conflicts with existing allowed_roots")
        defaults["durable-dispatch"] = {
            "enabled": True, "engine_root": str(deploy),
            "queue_root": str(config.parent.resolve() / "state" / "analysis-queue"),
            "allowed_roots": [str(source.resolve())],
        }
    updated = original
    data = yaml.safe_load(original) or {}
    for plugin, settings in defaults.items():
        if opted_out(data, plugin):
            continue
        for key, value in settings.items():
            updated = add_missing(updated, ["plugins", "entries", plugin, "settings", key], value)
    for plugin, toolset in (
        ("task-status", "task_status"),
        ("memory-bridge", "memory_bridge"),
        ("durable-dispatch", "durable_dispatch"),
        ("markdown-delivery", "markdown_delivery"),
        ("colleague-catalog", "colleague_catalog"),
        ("colleague-team", "colleague_team"),
    ):
        updated = ensure_toolset(updated, plugin, toolset)
    if updated == original:
        print("  默认能力配置已就位")
        return
    # Never replace credentials or comments and preserve permissions.
    fd, temp = tempfile.mkstemp(prefix=".config-defaults-", dir=config.parent)
    try:
        os.fchmod(fd, config.stat().st_mode & 0o777)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(updated)
            out.flush()
            os.fsync(out.fileno())
        shutil.copy2(config, config.with_name(config.name + ".before-defaults"))
        os.replace(temp, config)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    print("  已补齐长任务状态、进度和图文提示的缺省配置；已有设置保持不变")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, yaml.YAMLError) as exc:
        sys.exit("默认配置失败（原配置未修改）: " + str(exc))
