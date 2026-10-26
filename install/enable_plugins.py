#!/usr/bin/env python3
"""Idempotently enable installed plugins in a Hermes profile's ``config.yaml``.

Why a text merge instead of a YAML round-trip
---------------------------------------------
Hermes' official entry point is ``hermes plugins enable <name>``; install.sh uses it
when the target profile already exists. This script is the fallback for a fresh
profile (``hermes --profile`` refuses a profile without an identity marker) and for
hosts without the CLI. A YAML load/save round-trip would rewrite the whole file:
comments, key order and inline secrets all change. The installer promises never to
overwrite an existing config, so this merge edits *only* the ``plugins.enabled`` /
``plugins.disabled`` list lines; every other byte stays identical.

Safety
------
* Conservative scanner: anything it cannot prove it understands (flow mapping with
  content, duplicate top-level ``plugins:``, ``plugins`` as a scalar or sequence,
  tabs, multi-line flow lists) is refused with exit code 2 and a reason, leaving the
  file untouched.
* Writes are atomic (temp file + ``os.replace``) with the original mode preserved and
  a timestamped backup next to the config.
* ``--check`` and ``--dry-run`` never write.

Machine-readable stdout: one ``STATUS<TAB>value`` line per decision.
  STRUCTURE  missing|mapping|null|empty-flow|unsupported
  PRESENT / ABSENT / DISABLED  <name>          (``--check``)
  ADD / ALREADY / UNDISABLED  <name>           (merge)
  WOULD-ADD / WOULD-UNDISABLE  <name>          (merge --dry-run)
  CONFIG  created|updated|unchanged            (merge)
  WOULD-CONFIG  create|update                  (merge --dry-run)

Exit codes: 0 = success / already in place, 1 = I/O or self-check failure,
2 = unsupported structure (file untouched).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path


class Unsupported(Exception):
    """The existing config has a shape this minimal merge must not rewrite."""


def emit(status: str, value: str = "") -> None:
    sys.stdout.write(f"{status}\t{value}\n" if value else f"{status}\n")
    sys.stdout.flush()


def warn(message: str) -> None:
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


# --------------------------------------------------------------------------- #
# line scanning
# --------------------------------------------------------------------------- #
def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _blank(line: str) -> bool:
    return not line.strip()


def _comment(line: str) -> bool:
    return line.lstrip().startswith("#")


def _has_tab_indent(line: str) -> bool:
    return "\t" in line[: len(line) - len(line.lstrip(" \t"))]


def _split_comment(line: str) -> tuple[str, str]:
    """Split a line into (content, trailing comment including '#')."""
    quote = None
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index].rstrip(), line[index:]
    return line.rstrip(), ""


def _strip_comment(text: str) -> str:
    return _split_comment(text)[0].strip()


def _with_comment(content: str, comment: str) -> str:
    return f"{content} {comment}" if comment else content


_HEAD = re.compile(r"^plugins\s*:(.*)$")


def _find_top(lines: list[str]) -> int | None:
    hits = [index for index, line in enumerate(lines) if _HEAD.match(line)]
    if len(hits) > 1:
        raise Unsupported("duplicate top-level 'plugins:' key")
    return hits[0] if hits else None


def _block_end(lines: list[str], start: int) -> int:
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if _blank(line) or _comment(line):
            continue
        if line[0] in " \t":
            continue
        return index
    return len(lines)


def _child_indent(lines: list[str], start: int, end: int) -> int:
    for index in range(start + 1, end):
        line = lines[index]
        if _blank(line) or _comment(line):
            continue
        if _has_tab_indent(line):
            raise Unsupported("tab indentation")
        if line.lstrip() == "-" or line.lstrip().startswith("- "):
            raise Unsupported("'plugins' is a sequence")
        return _indent(line)
    return 2


def _shape(lines: list[str]) -> dict:
    top = _find_top(lines)
    if top is None:
        return {"kind": "missing", "top": None, "end": None}
    end = _block_end(lines, top)
    raw = _strip_comment(_HEAD.match(lines[top]).group(1))
    children = any(not _blank(line) and not _comment(line) for line in lines[top + 1:end])
    if raw in ("", "{}"):
        if not children:
            return {"kind": "null" if raw == "" else "empty-flow", "top": top, "end": end}
        if raw != "":
            raise Unsupported("'plugins: {}' also has children")
        return {"kind": "mapping", "top": top, "end": end,
                "child": _child_indent(lines, top, end)}
    if raw in ("~", "null", "Null", "NULL"):
        if children:
            raise Unsupported("'plugins' has both a null value and children")
        return {"kind": "null", "top": top, "end": end}
    raise Unsupported("unsupported 'plugins:' value: " + raw)


def _find_child(lines: list[str], start: int, end: int, key: str, indent: int) -> int | None:
    pattern = re.compile(r"^(\s*)%s\s*:(.*)$" % re.escape(key))
    hits = [index for index in range(start + 1, end)
            if (match := pattern.match(lines[index])) and len(match.group(1)) == indent]
    if len(hits) > 1:
        raise Unsupported(f"duplicate 'plugins.{key}' key")
    return hits[0] if hits else None


_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def _scalar(text: str) -> tuple[str | None, bool]:
    token = text.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        token = token[1:-1]
    if _NAME.match(token):
        return token, True
    return None, False


def _flow_items(raw: str) -> list[str]:
    inner = raw[1:-1].strip()
    if not inner:
        return []
    pieces, buffer, quote = [], "", None
    for char in inner:
        if quote:
            buffer += char
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
            buffer += char
        elif char == ",":
            pieces.append(buffer)
            buffer = ""
        else:
            buffer += char
    pieces.append(buffer)
    names = []
    for piece in pieces:
        name, simple = _scalar(piece)
        if not simple:
            raise Unsupported("non-scalar flow sequence item: " + piece.strip())
        names.append(name)
    return names


def _sequence(lines: list[str], key: int, end: int) -> dict:
    """Describe the value of a list key: mode, scalar items and their line ranges."""
    key_indent = _indent(lines[key])
    raw = _strip_comment(re.match(r"^\s*[^:]+:(.*)$", lines[key]).group(1))
    if raw.startswith("["):
        if not raw.endswith("]"):
            raise Unsupported("multi-line flow sequence")
        return {"mode": "flow", "names": _flow_items(raw), "items": [], "insert_at": key + 1}
    if raw not in ("", "~", "null", "Null", "NULL"):
        raise Unsupported("unsupported sequence value: " + raw)
    items = []
    index = key + 1
    while index < end:
        line = lines[index]
        if _blank(line) or _comment(line):
            index += 1
            continue
        indent = _indent(line)
        stripped = line.lstrip()
        if (stripped == "-" or stripped.startswith("- ")) and indent >= key_indent:
            token = _strip_comment(stripped[1:])
            name, simple = _scalar(token)
            stop = index + 1
            while stop < end:
                follower = lines[stop]
                if _blank(follower) or _comment(follower):
                    break
                if _indent(follower) > indent:
                    stop += 1
                    continue
                break
            items.append({"start": index, "stop": stop, "name": name if simple else None})
            index = stop
            continue
        if indent > key_indent:
            index += 1  # continuation already covered above; defensive
            continue
        break
    return {
        "mode": "block" if items else "null",
        "names": [item["name"] for item in items if item["name"] is not None],
        "items": items,
        "insert_at": items[-1]["stop"] if items else key + 1,
        "item_indent": _indent(lines[items[0]["start"]]) if items else key_indent,
    }


def _key_line(indent: int, key: str, value: str, comment: str = "") -> str:
    return _with_comment(f"{' ' * indent}{key}: {value}".rstrip(), comment)


def _item_line(indent: int, name: str) -> str:
    return f"{' ' * indent}- {name}"


# --------------------------------------------------------------------------- #
# merge
# --------------------------------------------------------------------------- #
def _apply(lines: list[str], edits: list[tuple[int, int, list[str]]]) -> list[str]:
    """Apply (start, stop, replacement) edits without index drift.

    Edits are expressed against the ORIGINAL line numbers: ranges are marked for
    deletion, replacements/insertions are attached to their start position, and
    the file is rebuilt in one pass. Several insertions at the same position keep
    their plan order (that is how a new ``enabled:`` block lands before a
    rewritten ``disabled: []`` sibling without either index shifting).
    """
    delete: set[int] = set()
    insert: dict[int, list[str]] = {}
    for start, stop, replacement in edits:
        for index in range(start, stop):
            if index in delete:
                raise RuntimeError(f"overlapping edits at line {index}")
            delete.add(index)
        insert.setdefault(start, []).extend(replacement)
    out: list[str] = []
    for index, line in enumerate(lines):
        if index in insert:
            out.extend(insert[index])
        if index not in delete:
            out.append(line)
    out.extend(insert.get(len(lines), []))
    return out


def _fresh_text(names: list[str]) -> str:
    out = ["# Written by install/install.sh: plugin enable list only; no credentials here.",
           "plugins:", "  enabled:"]
    out.extend(f"  - {name}" for name in names)
    return "\n".join(out) + "\n"


def merge(text: str, names: list[str]) -> tuple[str, list[tuple[str, str]]]:
    """Return (new_text, decisions). Raises Unsupported for unsafe shapes."""
    decisions: list[tuple[str, str]] = []
    ends_with_newline = text.endswith("\n")
    lines = text.splitlines()
    shape = _shape(lines)
    kind = shape["kind"]

    if kind == "missing":
        body = _fresh_text(names).splitlines()
        if lines:
            if lines[-1].strip():
                lines.append("")
            lines.extend(body)
        else:
            lines = body
            ends_with_newline = True
        decisions.extend(("ADD", name) for name in names)
        decisions.append(("CONFIG", "created" if not text.strip() else "updated"))
        return "\n".join(lines) + ("\n" if ends_with_newline else ""), decisions

    top = shape["top"]
    edits: list[tuple[int, int, list[str]]] = []

    if kind in ("null", "empty-flow"):
        key_comment = _split_comment(lines[top])[1]
        replacement = [_with_comment("plugins:", key_comment), "  enabled:"]
        replacement.extend(_item_line(2, name) for name in names)
        edits.append((top, top + 1, replacement))
        decisions.extend(("ADD", name) for name in names)
        decisions.append(("CONFIG", "updated"))
    else:
        child = shape["child"]
        enabled_at = _find_child(lines, top, shape["end"], "enabled", child)
        disabled_at = _find_child(lines, top, shape["end"], "disabled", child)

        # ---- enabled: add missing names -------------------------------------
        enabled_names: list[str] = []
        if enabled_at is not None:
            sequence = _sequence(lines, enabled_at, shape["end"])
            enabled_names = sequence["names"]
        missing = [name for name in names if name not in enabled_names]

        if missing:
            if enabled_at is None:
                # ``enabled:`` with no value; keep the host's indentless-sequence style.
                insertion = [f"{' ' * child}enabled:"]
                insertion.extend(_item_line(child, name) for name in missing)
                edits.append((top + 1, top + 1, insertion))
            else:
                sequence = _sequence(lines, enabled_at, shape["end"])
                key_comment = _split_comment(lines[enabled_at])[1]
                if sequence["mode"] == "flow":
                    replacement = [_with_comment(f"{' ' * _indent(lines[enabled_at])}enabled:",
                                                 key_comment)]
                    replacement.extend(_item_line(_indent(lines[enabled_at]), name)
                                       for name in [*sequence["names"], *missing])
                    edits.append((enabled_at, enabled_at + 1, replacement))
                elif sequence["mode"] == "null":
                    insertion = [_item_line(_indent(lines[enabled_at]), name) for name in missing]
                    edits.append((enabled_at + 1, enabled_at + 1, insertion))
                else:
                    insertion = [_item_line(sequence["item_indent"], name) for name in missing]
                    at = sequence["insert_at"]
                    edits.append((at, at, insertion))
            decisions.extend(("ADD", name) for name in missing)
        else:
            decisions.extend(("ALREADY", name) for name in names)

        # ---- disabled: remove names that would veto the enable ---------------
        removed: list[str] = []
        if disabled_at is not None:
            sequence = _sequence(lines, disabled_at, shape["end"])
            removed = [name for name in sequence["names"] if name in names]
            if removed:
                key_indent = _indent(lines[disabled_at])
                key_comment = _split_comment(lines[disabled_at])[1]
                if sequence["mode"] == "flow":
                    remaining = [name for name in sequence["names"] if name not in names]
                    if remaining:
                        replacement = [_with_comment(f"{' ' * key_indent}disabled:", key_comment)]
                        replacement.extend(_item_line(key_indent, name) for name in remaining)
                    else:
                        replacement = [_key_line(key_indent, "disabled", "[]", key_comment)]
                    edits.append((disabled_at, disabled_at + 1, replacement))
                else:
                    remaining_items = [item for item in sequence["items"]
                                       if item["name"] is None or item["name"] not in names]
                    for item in sequence["items"]:
                        if item["name"] is not None and item["name"] in names:
                            edits.append((item["start"], item["stop"], []))
                    if not remaining_items:
                        edits.append((disabled_at, disabled_at + 1,
                                      [_key_line(key_indent, "disabled", "[]", key_comment)]))
                decisions.extend(("UNDISABLED", name) for name in removed)

        if missing or removed:
            decisions.append(("CONFIG", "updated"))
        else:
            decisions.append(("CONFIG", "unchanged"))

    new_lines = _apply(list(lines), edits)
    new_text = "\n".join(new_lines) + ("\n" if ends_with_newline else "")

    # ---- self-check: nothing outside the plugins block may change -----------
    old_tail = lines[shape["end"]:]
    new_shape = _shape(new_lines)
    if new_shape["kind"] != "mapping":
        raise RuntimeError("merge did not produce a plugins mapping")
    new_tail = new_lines[new_shape["end"]:]
    if lines[: top] != new_lines[: top] or old_tail != new_tail:
        raise RuntimeError("merge touched lines outside the plugins block")
    check = inspect(new_text)
    if any(name not in check["enabled"] for name in names):
        raise RuntimeError("merge did not add every requested name")
    if set(check["disabled"]) & set(names):
        raise RuntimeError("merge left a requested name in plugins.disabled")
    return new_text, decisions


def inspect(text: str) -> dict:
    """Read-only view of the plugin enable lists."""
    lines = text.splitlines()
    shape = _shape(lines)
    if shape["kind"] == "missing":
        return {"structure": "missing", "enabled": [], "disabled": []}
    if shape["kind"] != "mapping":
        return {"structure": shape["kind"], "enabled": [], "disabled": []}
    child = shape["child"]
    enabled_at = _find_child(lines, shape["top"], shape["end"], "enabled", child)
    disabled_at = _find_child(lines, shape["top"], shape["end"], "disabled", child)
    enabled = _sequence(lines, enabled_at, shape["end"])["names"] if enabled_at is not None else []
    disabled = _sequence(lines, disabled_at, shape["end"])["names"] if disabled_at is not None else []
    return {"structure": "mapping", "enabled": enabled, "disabled": disabled}


# --------------------------------------------------------------------------- #
# write
# --------------------------------------------------------------------------- #
def _write_atomic(path: Path, text: str) -> None:
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".config.yaml.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _backup(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.bak.{stamp}-{os.getpid()}")
    shutil.copy2(path, target)
    return target


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True, help="profile config.yaml path")
    parser.add_argument("--name", action="append", default=[], help="plugin enable name (repeatable)")
    parser.add_argument("--check", action="store_true", help="report current state; never write")
    parser.add_argument("--dry-run", action="store_true", help="report the plan; never write")
    args = parser.parse_args(argv)

    names = [name for name in args.name if name]
    if not names and not args.check:
        warn("enable_plugins: no --name given")
        return 1

    path = Path(args.config)
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as error:
        warn(f"enable_plugins: cannot read {path}: {error}")
        return 1

    try:
        current = inspect(text)
    except Unsupported as error:
        emit("STRUCTURE", "unsupported")
        warn(f"enable_plugins: {path}: {error}")
        return 2

    if args.check:
        emit("STRUCTURE", current["structure"])
        for name in names:
            if name in current["disabled"]:
                emit("DISABLED", name)
            elif name in current["enabled"]:
                emit("PRESENT", name)
            else:
                emit("ABSENT", name)
        return 0

    try:
        new_text, decisions = merge(text, names)
    except Unsupported as error:
        emit("STRUCTURE", "unsupported")
        warn(f"enable_plugins: refusing to edit {path}: {error}")
        return 2

    changed = new_text != text
    if args.dry_run:
        for status, value in decisions:
            if status == "ADD":
                emit("WOULD-ADD", value)
            elif status == "UNDISABLED":
                emit("WOULD-UNDISABLE", value)
            elif status == "CONFIG":
                emit("WOULD-CONFIG", "create" if value == "created" else
                     "update" if value == "updated" else "unchanged")
            else:
                emit(status, value)
        return 0

    if changed:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                backup = _backup(path)
                warn(f"enable_plugins: backup {backup}")
            _write_atomic(path, new_text)
        except OSError as error:
            warn(f"enable_plugins: cannot write {path}: {error}")
            return 1

    for status, value in decisions:
        emit(status, value)
    return 0


if __name__ == "__main__":
    sys.exit(main())
