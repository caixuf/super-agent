#!/usr/bin/env python3
"""宿主侧补丁：卡片原生表格 + 卡片 JSON 2.0 开关（幂等，带在位探针）。

补的是什么（2026-10-27 实测定性）：

1. 卡片（JSON 2.0）在根节点支持 ``tag: table``，标题/加粗/行内码本来就能渲染。
   旧逻辑见表格就把整条回复回退成 post，等于放弃了卡片形态。补丁把 GFM 管道表格拆成
   卡片原生表格（``lark_md`` 列、``row_height=low``——``auto`` 会被平台拒：200905），
   只有卡片表达不了的东西（引用块、分割线、围栏代码、超过 5 张表）才回退 post。
2. 2.0 卡片需要客户端 ≥ 7.20，旧客户端只显示「请升级客户端」。所以做成能力开关
   ``HERMES_FEISHU_CARD_V2``（默认关 = 保持旧行为，开 = 卡片按 2.0 发）。
3. 卡片的 PATCH 要求**更新前后都是共享卡片**（``config.update_multi: true``）。流式帧
   以前丢了 ``config``，每一帧都在造非共享卡 —— 补丁让每一帧都带 ``update_multi``。
4. 部分客户端/接口走 **card_components v2（'卡片 2.4'）**，它拒绝 1.0 的 ``markdown`` 与
   ``note`` 标签（实测 ``200861 unsupported tag note``，整张卡回退纯文本）。开关
   ``HERMES_FEISHU_CARD_24=1`` 打开时，正文改 ``plain_text``、表格单元格改 ``text``、
   运行时长提示改 ``plain_text``；关掉即回到 2.0 的 ``markdown`` + 原生表格。

用法：

    HERMES_AGENT_DIR=~/.hermes/hermes-agent python3 patches/patch_card_markdown_native.py --check
    HERMES_AGENT_DIR=~/.hermes/hermes-agent python3 patches/patch_card_markdown_native.py --apply
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ADAPTER = "plugins/platforms/feishu/adapter.py"

ANCHOR_AFTER = '''def _markdown_needs_post(text: str) -> bool:
    """Feishu card markdown degrades tables/headings/quotes/fences; use a post message."""
    return bool(_CARD_MARKDOWN_COMPLEX_RE.search(text or ""))
'''

HELPERS = '''

# Cards accepted these structures natively (JSON 2.0) long ago, so a reply containing
# headings or tables no longer has to leave the card as a post. Only what JSON 2.0 cannot
# express here (block quotes, horizontal rules, fenced code, >5 tables) still falls back.
_CARD_UNSUPPORTED_RE = re.compile(r"(?m)(^\\s*>\\s+|^\\s*---+\\s*$|^\\s*```|^\\s*~~~)")
_CARD_TABLE_HEADER_RE = re.compile(r"^\\s*\\|.*\\|\\s*$")
_CARD_TABLE_DIVIDER_RE = re.compile(r"^\\s*\\|?\\s*:?-{2,}:?\\s*(\\|\\s*:?-{2,}:?\\s*)+\\|?\\s*$")
_CARD_TABLE_MAX = 5
_CARD_V2_ENV = "HERMES_FEISHU_CARD_V2"
_CARD24_ENV = "HERMES_FEISHU_CARD_24"


def _card_v2_enabled() -> bool:
    """Native tables need card JSON 2.0, which older clients render as '请升级客户端'.

    Opt-in per deployment: unset keeps the previous behaviour (complex markdown -> post).
    """
    return (os.environ.get(_CARD_V2_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def _md_table_cells(line: str) -> list[str]:
    return [cell.strip().replace("\\\\|", "|") for cell in line.strip().strip("|").split("|")]


def _card_tables(lines: list[str]) -> list[tuple[int, int, dict[str, Any]]]:
    """Locate GFM pipe tables: ``[(start, stop, element)]``; ``stop`` is exclusive."""
    found: list[tuple[int, int, dict[str, Any]]] = []
    index = 0
    while index < len(lines) - 1:
        line = lines[index]
        if not (_CARD_TABLE_HEADER_RE.match(line) and _CARD_TABLE_DIVIDER_RE.match(lines[index + 1])):
            index += 1
            continue
        stop = index + 2
        while stop < len(lines) and _CARD_TABLE_HEADER_RE.match(lines[stop]):
            stop += 1
        if len(found) < _CARD_TABLE_MAX:
            header = _md_table_cells(line)
            rows = [{"c%d" % n: (row[n] if n < len(row) else "")
                     for n in range(len(header))}
                    for row in (_md_table_cells(text) for text in lines[index + 2:stop])]
            found.append((index, stop, {
                "tag": "table", "page_size": 10, "row_height": "low",
                "freeze_first_column": len(header) > 2,
                "header_style": {"text_size": "normal", "background_style": "grey", "bold": True},
                "columns": [{"name": "c%d" % n, "display_name": header[n] or " ",
                             "data_type": "lark_md", "width": "auto", "vertical_align": "top"}
                            for n in range(len(header))],
                "rows": rows,
            }))
        index = stop
    return found


def build_card_elements(content: str) -> list[dict[str, Any]]:
    """One markdown reply -> card elements: markdown blocks with native tables in place."""
    if not content:
        return [{"tag": "markdown", "content": " "}]
    lines = content.splitlines()
    elements: list[dict[str, Any]] = []
    cursor = 0
    for start, stop, table in _card_tables(lines):
        prose = "\\n".join(lines[cursor:start])
        if prose.strip():
            elements.append({"tag": "markdown", "content": prose})
        elements.append(table)
        cursor = stop
    tail = "\\n".join(lines[cursor:])
    if tail.strip() or not elements:
        elements.append({"tag": "markdown", "content": tail or " "})
    return elements


_CARD24_INLINE_MD_RE = re.compile(r"\\*\\*(.+?)\\*\\*|`([^`]+)`|\\[([^\\]]+)\\]\\(([^)]+)\\)")


def _card24_text(content: str) -> str:
    """Markdown -> the plain-text subset card_components v2 accepts (no markdown tag)."""
    text = _CARD24_INLINE_MD_RE.sub(
        lambda m: m.group(1) or m.group(2) or ("%s (%s)" % (m.group(3), m.group(4))), content or "")
    text = text.replace("**", "").replace("`", "")
    return re.sub(r"(?m)^#{1,6}\\s+", "", text)


def card_components_v2() -> bool:
    """Card 2.4 ('card_components' v2) rejects the 1.0 ``markdown``/``note`` tags.

    Opt-in per deployment; the 2.0 path (markdown + table components) stays the default.
    """
    return (os.environ.get(_CARD24_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def build_card24_elements(content: str) -> list[dict[str, Any]]:
    """Same split as the 2.0 builder: plain_text blocks, tables with plain-text cells."""
    elements = build_card_elements(content)
    for element in elements:
        if element.get("tag") != "table":
            continue
        for column in element["columns"]:
            column["data_type"] = "text"
        for row in element["rows"]:
            for column in element["columns"]:
                row[column["name"]] = _card24_text(str(row.get(column["name"], "")))
    return [element if element.get("tag") == "table"
            else {"tag": "plain_text", "content": _card24_text(element.get("content", "")) or " "}
            for element in elements]


def card_renders_markdown(text: str) -> bool:
    """True when every construct in *text* has a native card representation."""
    text = text or ""
    if _CARD_UNSUPPORTED_RE.search(text):
        return False
    lines = text.splitlines()
    # A table beyond the five-per-card limit keeps its divider rows as prose: count those, too.
    dividers = sum(1 for index in range(len(lines) - 1)
                   if _CARD_TABLE_HEADER_RE.match(lines[index])
                   and _CARD_TABLE_DIVIDER_RE.match(lines[index + 1]))
    return dividers == len(_card_tables(lines))
'''

SEND_OLD = '''        if (getattr(self, "_card_replies", False) and (metadata or {}).get("expect_edits")
                and not any(_markdown_needs_post(chunk) for chunk in chunks)):'''
SEND_NEW = '''        if (getattr(self, "_card_replies", False) and _card_v2_enabled()
                and (metadata or {}).get("expect_edits")
                and not any(not card_renders_markdown(chunk) for chunk in chunks)):'''
SEND_PROBE = "and not any(not card_renders_markdown(chunk) for chunk in chunks)):"

EDIT_OLD = '''        if getattr(self, "_card_replies", False) and self._is_card_message(message_id):
            if _markdown_needs_post(content):'''
EDIT_NEW = '''        if getattr(self, "_card_replies", False) and _card_v2_enabled() and self._is_card_message(message_id):
            if not card_renders_markdown(content):'''
EDIT_PROBE = "if not card_renders_markdown(content):"

BUILD_OLD = '''        name = (str(getattr(self, "_bot_name", "") or "Hermes")).strip() or "Hermes"
        elements: list[dict[str, Any]] = [{"tag": "markdown", "content": content or " "}]'''
BUILD_OLD_V11 = '''        # Markdown 里有没有卡片表达不了的构造由调用方先判断；这里按原生表格拆元素。
        elements: list[dict[str, Any]] = build_card_elements(content)'''
BUILD_NEW = '''        name = (str(getattr(self, "_bot_name", "") or "Hermes")).strip() or "Hermes"
        # Markdown 里有没有卡片表达不了的构造由调用方先判断；这里按原生表格拆元素。
        # 卡片 2.4 只认 plain_text / table，2.0 认 markdown 块。
        elements: list[dict[str, Any]] = (build_card24_elements(content) if card_components_v2()
                                          else build_card_elements(content))'''
BUILD_PROBE = "if card_components_v2()\n"

#: The streaming tip is a 1.0 ``note``; 2.4 rejects it (200861) and the whole card falls
#: back to plain text. Rewritten in both tree shapes.
NOTE_OLD = '''            elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": tip}]})'''
NOTE_NEW = '''            if card_components_v2():
                # 'note' is a 1.0 tag; 2.4 rejects it (200861) and falls back to plain text.
                elements.append({"tag": "plain_text", "content": tip})
            else:
                elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": tip}]})'''
NOTE_PROBE = "if card_components_v2():\n                # 'note' is a 1.0 tag"

RETURN_OLD = '''        return {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": name},
                       "template": "green" if finalize else "blue"},
            "elements": elements,
        }'''
RETURN_OLD_V11 = '''        header = {"title": {"tag": "plain_text", "content": name},
                  "template": "green" if finalize else "blue"}
        return {"schema": "2.0", "config": {"wide_screen_mode": True, "update_multi": True},
                "header": header, "body": {"elements": elements}}'''
RETURN_NEW = '''        header = {"title": {"tag": "plain_text", "content": name},
                  "template": "green" if finalize else "blue"}
        if card_components_v2():
            # 2.4 rejects the 1.0 markdown/note tags; plain_text and table are accepted.
            card: dict[str, Any] = {"schema": "2.0", "header": header,
                                    "body": {"elements": elements}}
            card["config"] = {"update_multi": True}
        elif _card_v2_enabled():
            # update_multi stays true on every frame: the PATCH API requires a shared card
            # before AND after each update, and the streaming frames are exactly those updates.
            card = {"schema": "2.0", "config": {"wide_screen_mode": True, "update_multi": True},
                    "header": header, "body": {"elements": elements}}
        else:
            return {"config": {"wide_screen_mode": True}, "header": header, "elements": elements}
        return card'''
RETURN_NEW_V11 = RETURN_NEW.replace('''        header = {"title": {"tag": "plain_text", "content": name},
                  "template": "green" if finalize else "blue"}
''', '')
RETURN_PROBE = "if card_components_v2():"


#: signature of the first published version of the helper block (before the 2.4 branch).
V11_HELPERS = HELPERS.split("_CARD24_INLINE_MD_RE = re.compile(")[0].rstrip() + "\n"


def _strip_24(value: str) -> str:
    """Drop the 2.4 branch lines from generated code (keeps the v1.1 shape when absent)."""
    return "".join(line for line in value.splitlines(True) if "card_components_v2" not in line)

#: (name, old, new, probe). One entry per step; a step is done when its probe is present and
#: its own target shape no longer matches. Two trees exist in the wild: pristine (markdown
#: block per card) and v1.1 (native tables, 2.0 payload) — chain-safe shapes.
EDITS = [
    ("helpers", ANCHOR_AFTER, ANCHOR_AFTER + HELPERS, "def _card_v2_enabled() -> bool:"),
    # Upgrade a tree patched by the first version of this script (helpers, no 2.4 branch).
    ("helpers-24", V11_HELPERS, HELPERS, "def card_components_v2() -> bool:"),
    ("send-gate", SEND_OLD, SEND_NEW, SEND_PROBE),
    ("edit-gate", EDIT_OLD, EDIT_NEW, EDIT_PROBE),
    ("build", BUILD_OLD, BUILD_NEW, BUILD_PROBE),
    ("build-v11", BUILD_OLD_V11, _strip_24(BUILD_NEW), BUILD_PROBE),
    ("tip", NOTE_OLD, NOTE_NEW, NOTE_PROBE),
    ("return", RETURN_OLD, RETURN_NEW, RETURN_PROBE),
    ("return-v11", RETURN_OLD_V11, _strip_24(RETURN_NEW_V11), RETURN_PROBE),
]


def _agent_dir(explicit):
    root = Path(explicit or os.environ.get("HERMES_AGENT_DIR") or "~/.hermes/hermes-agent")
    return root.expanduser()


def _missing(text):
    """Not in place = probe absent, or the unchanged anchor still present.

    When the probe is present the edit's *new* text is in the file, so the old anchor can
    only survive where the new text already contains it (chained anchors such as
    ``ANCHOR_AFTER + helpers``) — that is not a miss.
    """
    # An edit that already has its text in place is done, even when its old anchor is still
    # there (chained anchors) — except for the no-op "v1.1" markers, whose job is only to be
    # superseded once the real edit applied.
    return [(old, probe) for _name, old, _new, probe in EDITS if probe not in text]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--apply", action="store_true")
    ap.add_argument("--agent-dir", default=None)
    args = ap.parse_args()

    path = _agent_dir(args.agent_dir) / ADAPTER
    if not path.is_file():
        print("[FAIL] 找不到 %s" % path)
        return 2
    text = path.read_text(encoding="utf-8")
    missing = _missing(text)
    if args.check:
        if not missing:
            print("[PASS] 卡片原生表格补丁：在位（%d 处）" % len(EDITS))
            return 0
        print("[FAIL] 卡片原生表格补丁：不在位（缺 %d 处）" % len(missing))
        for _old, probe in missing:
            print("  - 缺 %r" % probe[:70])
        return 1
    if not missing:
        print("[PASS] 已在位，无需重放")
        return 0
    # Pass 1 assembles the final text from the ORIGINAL file; pass 2 writes it. An earlier
    # step can therefore absorb a later step's anchor (helpers carry their own markers)
    # without the later pass having to rewrite text it just produced.
    for index, (name, old, new, probe) in enumerate(EDITS):
        if probe in text and old not in text:
            continue                     # this step is done and no old shape is left to rewrite
        count = text.count(old)
        if count == 0:
            continue                     # this tree does not have this shape; another entry covers it
        if count != 1:
            print("[FAIL] %s: 锚点命中 %d 次（应为 1）——文件已漂移，请人工核对" % (name, count))
            return 2
        text = text.replace(old, new, 1)
        print("[OK] 已改 %s" % name)
    still = _missing(text)
    if still:
        print("[FAIL] 应用后仍缺 %d 处（本文件不支持该树形，请人工核对）" % len(still))
        return 2
    path.write_text(text, encoding="utf-8")
    print("[PASS] 补丁已应用；重启网关后生效")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
