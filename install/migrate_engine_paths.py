#!/usr/bin/env python3
"""Move the paths that point into the engine when the engine location changes.

A deployment that moves the engine (from a managed release directory to the
ordinary ``<HERMES_HOME>/agentcore`` layout) leaves a few configuration values
pointing at the old location:

- ``plugins.entries.<name>.settings.engine_root``
- ``plugins.entries.<name>.settings.delivery_transport_file``
- ``memory.localvec.recall_cmd`` entries that name the engine's own tool

The official installers deliberately never rewrite an existing entry, so this
one-shot migration belongs to the deployer.

The rule is deliberately narrow: only a value that **starts with** the old
engine root is rewritten, and only in the locations listed above. Site state
(queue roots, liveness ledger, memory data directories, credential paths) does
not live under the engine root and is therefore never touched. An unexpected
YAML shape aborts without touching the file.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import enable_plugins as config_text


def _block_end_at(lines: list[str], start: int, indent: int) -> int:
    """End of a nested block: the next line at or above the given indent."""
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if config_text._blank(line) or config_text._comment(line):
            continue
        if config_text._indent(line) <= indent:
            return index
    return len(lines)


def _top_key(lines: list[str], key: str) -> int | None:
    """Locate a document-level mapping key (column zero)."""
    for index, line in enumerate(lines):
        if config_text._comment(line) or config_text._blank(line):
            continue
        if config_text._indent(line) != 0:
            continue
        name, _ = config_text._scalar(config_text._strip_comment(line.split(":", 1)[0]))
        if name == key:
            return index
    return None


def _child_key(lines: list[str], start: int, end: int, indent: int, key: str) -> int | None:
    """Locate one direct child key of a mapping block."""
    for index in range(start + 1, end):
        line = lines[index]
        if config_text._comment(line) or config_text._blank(line):
            continue
        if config_text._indent(line) != indent:
            continue
        name, _ = config_text._scalar(config_text._strip_comment(line.split(":", 1)[0]))
        if name == key:
            return index
    return None


def _scalar_value(line: str) -> str | None:
    """The scalar text of ``key: value`` (quotes stripped), or None."""
    raw, _ = config_text._split_comment(line)
    if ":" not in raw:
        return None
    token = config_text._strip_comment(raw.split(":", 1)[1]).strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        token = token[1:-1]
    return token


def _item_value(item: str) -> str | None:
    """The scalar text of a sequence item, or None when it is not one."""
    stripped = item.strip()
    if not stripped.startswith("- "):
        return None
    token = stripped[2:].strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        token = token[1:-1]
    return token


def _rewrite(text: str, old: str, new: str, extra: list[tuple[str, str]] | None = None) -> tuple[str, list[str]]:
    """Rewrite engine-internal paths; return the new text and what changed."""
    lines = text.splitlines()
    shape = config_text._shape(lines)
    if shape["kind"] != "mapping":
        raise config_text.Unsupported("config must be a mapping")
    changed: list[str] = []
    prefix = old.rstrip("/") + "/"

    def replacement(value: str) -> str | None:
        """The new value for one path, or None when it must stay as it is."""
        for source, destination in extra or []:
            if value == source:
                return destination
        if value == old:
            return new
        if value.startswith(prefix):
            return new + value[len(old):]
        return None

    # plugins.entries.<name>.settings.<key>
    entries = config_text._find_child(lines, shape["top"], shape["end"], "entries", shape["child"])
    if entries is not None:
        entries_end = config_text._block_end(lines, entries)
        entry_indent = config_text._child_indent(lines, entries, entries_end)
        for index in range(entries + 1, entries_end):
            line = lines[index]
            if config_text._comment(line) or config_text._blank(line):
                continue
            if config_text._indent(line) != entry_indent:
                continue
            name, _ = config_text._scalar(config_text._strip_comment(line.split(":", 1)[0]))
            if name is None:
                raise config_text.Unsupported("ambiguous plugin entry key")
            entry_end = _block_end_at(lines, index, entry_indent)
            settings = _child_key(lines, index, entry_end, entry_indent + 2, "settings")
            if settings is None:
                continue
            settings_end = _block_end_at(lines, settings, entry_indent + 2)
            setting_indent = config_text._child_indent(lines, settings, settings_end)
            for key_index in range(settings + 1, settings_end):
                key_line = lines[key_index]
                if config_text._comment(key_line) or config_text._blank(key_line):
                    continue
                if config_text._indent(key_line) != setting_indent:
                    continue
                value = _scalar_value(key_line)
                target = replacement(value) if value is not None else None
                if target is None:
                    continue
                _, comment = config_text._split_comment(key_line)
                key_name = config_text._strip_comment(key_line.split(":", 1)[0]).strip()
                lines[key_index] = config_text._key_line(
                    setting_indent, key_name, json.dumps(target), comment)
                changed.append("plugins.entries." + name + ".settings." + key_name)

    # memory.localvec.recall_cmd list items
    localvec = _top_key(lines, "memory")
    if localvec is not None:
        memory_end = _block_end_at(lines, localvec, 0)
        localvec_index = _child_key(lines, localvec, memory_end, 2, "localvec")
        if localvec_index is not None:
            localvec_end = _block_end_at(lines, localvec_index, 2)
            recall = _child_key(lines, localvec_index, localvec_end, 4, "recall_cmd")
            if recall is not None:
                # A YAML sequence may sit at the same indentation as its key.
                recall_end = _block_end_at(lines, recall, 3)
                for index in range(recall + 1, recall_end):
                    item = lines[index]
                    if config_text._comment(item) or config_text._blank(item):
                        continue
                    stripped = item.strip()
                    if not stripped.startswith("- "):
                        continue
                    value = _item_value(item)
                    target = replacement(value) if value is not None else None
                    if target is None:
                        continue
                    _, comment = config_text._split_comment(item)
                    lines[index] = config_text._with_comment(
                        " " * config_text._indent(item) + "- " + json.dumps(target),
                        comment)
                    changed.append("memory.localvec.recall_cmd")

    if not changed:
        return text, []
    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--old-engine-root", required=True)
    parser.add_argument("--new-engine-root", required=True)
    parser.add_argument("--replace", action="append", default=[],
                        metavar="OLD=NEW",
                        help="exact path replacement (repeatable); use for paths outside the engine root")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.config.is_file():
            print("migrate_engine_paths: config not found", file=sys.stderr)
            return 2
        text = args.config.read_text(encoding="utf-8")
        extra: list[tuple[str, str]] = []
        for item in args.replace:
            source, separator, destination = item.partition("=")
            if not separator or not source or not destination:
                print("migrate_engine_paths: --replace expects OLD=NEW", file=sys.stderr)
                return 2
            extra.append((source, destination))
        updated, changed = _rewrite(text, args.old_engine_root, args.new_engine_root, extra)
        if not changed:
            print("  [--] 没有指向旧引擎内部的路径（保持原样）")
            return 0
        for name in changed:
            print("    [plan] " + name + " -> " + args.new_engine_root)
        if args.dry_run:
            return 0
        config_text._backup(args.config)
        config_text._write_atomic(args.config, updated)
        print("  [OK] 已迁移 " + str(len(changed)) + " 处引擎内路径")
        return 0
    except (OSError, config_text.Unsupported) as error:
        print("migrate_engine_paths: refusing to rewrite (" + type(error).__name__ +
              "); config unchanged", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
