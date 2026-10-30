"""End-to-end test of patches/patch_card_markdown_native.py on a synthetic pristine adapter.

The patch is the only thing that puts the card helpers on a host, so it is tested the way it
runs: copy a minimal adapter shaped like the real one, apply, then exercise the helpers and
the emitted card payloads (2.0 markdown/table, 2.4 div/table, 1.0 fallback).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PATCH = REPO / "patches" / "patch_card_markdown_native.py"

PRISTINE = '''"""Minimal stand-in for the host adapter: only the anchors the patch needs."""
from __future__ import annotations

import json
import re
import time
from typing import Any

_CARD_MARKDOWN_COMPLEX_RE = re.compile(
    r"(?m)(^\\s*\\|.*\\|\\s*$|^\\s*#{1,6}\\s+|^\\s*>\\s+|^\\s*---+\\s*$|^\\s*```)")


def _markdown_needs_post(text: str) -> bool:
    """Feishu card markdown degrades tables/headings/quotes/fences; use a post message."""
    return bool(_CARD_MARKDOWN_COMPLEX_RE.search(text or ""))


def _coerce_int(value: Any, default=None) -> Any:
    return default


class Adapter:
    _card_replies = True

    def __init__(self, bot_name="bot"):
        self._bot_name = bot_name

    def _is_card_message(self, message_id):
        return True

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        chunks = [content]
        if (getattr(self, "_card_replies", False) and (metadata or {}).get("expect_edits")
                and not any(_markdown_needs_post(chunk) for chunk in chunks)):
            return "card"

    async def edit_message(self, chat_id, message_id, content, *, finalize=False, metadata=None):
        if getattr(self, "_card_replies", False) and self._is_card_message(message_id):
            if _markdown_needs_post(content):
                return "post"

    def _build_reply_card(self, content, *, finalize=False, started_at=None):
        name = (str(getattr(self, "_bot_name", "") or "Hermes")).strip() or "Hermes"
        elements: list[dict[str, Any]] = [{"tag": "markdown", "content": content or " "}]
        if not finalize:
            elapsed = max(0, int(time.time() - (started_at or time.time())))
            mins, secs = divmod(elapsed, 60)
            tip = f"\\u23f3 \\u8fd0\\u884c\\u4e2d {mins}\\u5206{secs:02d}\\u79d2" if mins else f"\\u23f3 \\u8fd0\\u884c\\u4e2d {secs}\\u79d2"
            elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": tip}]})
        return {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": name},
                       "template": "green" if finalize else "blue"},
            "elements": elements,
        }
'''


@pytest.fixture()
def patched(tmp_path, monkeypatch):
    for key in ("HERMES_FEISHU_CARD_24", "HERMES_FEISHU_CARD_V2"):
        monkeypatch.delenv(key, raising=False)
    tree = tmp_path / "agent"
    target = tree / "plugins/platforms/feishu/adapter.py"
    target.parent.mkdir(parents=True)
    target.write_text(PRISTINE, encoding="utf-8")
    env = dict(os.environ, HERMES_AGENT_DIR=str(tree))
    first = subprocess.run([sys.executable, str(PATCH), "--apply"], env=env,
                           capture_output=True, text=True)
    assert first.returncode == 0, first.stdout + first.stderr
    check = subprocess.run([sys.executable, str(PATCH), "--check"], env=env,
                           capture_output=True, text=True)
    assert check.returncode == 0, check.stdout
    again = subprocess.run([sys.executable, str(PATCH), "--apply"], env=env,
                           capture_output=True, text=True)
    assert again.returncode == 0 and "已在位" in again.stdout
    source = target.read_text(encoding="utf-8")
    assert "\\n" in source and "def card_components_v2" in source
    namespace = {"re": re, "os": os, "Any": object, "time": time}
    exec(compile(source, "<patched-adapter>", "exec"), namespace)
    namespace["Adapter"] = namespace["Adapter"]
    return namespace


def _card_for(namespace, text, *, finalize=True, card24=False):
    os.environ["HERMES_FEISHU_CARD_V2"] = "1"
    os.environ.pop("HERMES_FEISHU_CARD_24", None)
    if card24:
        os.environ["HERMES_FEISHU_CARD_24"] = "1"
    return namespace["Adapter"]()._build_reply_card(text, finalize=finalize)


def test_check_fails_before_apply(tmp_path):
    tree = tmp_path / "agent"
    target = tree / "plugins/platforms/feishu/adapter.py"
    target.parent.mkdir(parents=True)
    target.write_text(PRISTINE, encoding="utf-8")
    env = dict(os.environ, HERMES_AGENT_DIR=str(tree))
    check = subprocess.run([sys.executable, str(PATCH), "--check"], env=env,
                           capture_output=True, text=True)
    assert check.returncode == 1 and "不在位" in check.stdout


def test_table_reply_becomes_a_native_table_in_a_2_0_card(patched):
    card = _card_for(patched, "## 结论\n\n| 维度 | 新 |\n|---|---|\n| 速度 | 快 |\n")
    assert card["schema"] == "2.0"
    assert card["config"]["update_multi"] is True
    tags = [element["tag"] for element in card["body"]["elements"]]
    assert tags == ["markdown", "table"]
    assert card["body"]["elements"][1]["row_height"] == "low"
    assert card["body"]["elements"][1]["rows"] == [{"c0": "速度", "c1": "快"}]


def test_2_4_mode_uses_div_and_text_tables(patched):
    card = _card_for(patched, "## 结论\n\n| a | b |\n|---|---|\n| 1 | 2 |", finalize=False, card24=True)
    tags = [element["tag"] for element in card["body"]["elements"]]
    assert tags == ["div", "table", "div"]
    assert all(element["text"]["tag"] == "plain_text" for element in (card["body"]["elements"][0],
                                                                     card["body"]["elements"][2]))
    assert all(column["data_type"] == "text" for column in card["body"]["elements"][1]["columns"])


def test_plain_prose_uses_markdown_blocks_without_tables(patched):
    card = _card_for(patched, "现在的状态：已受理。")
    assert card["schema"] == "2.0"
    assert [element["tag"] for element in card["body"]["elements"]] == ["markdown"]


def test_unsupported_construct_flags_a_post(patched):
    assert patched["card_renders_markdown"]("普通 **粗体** 与 `code`")
    assert not patched["card_renders_markdown"]("```bash\necho 1\n```")
    assert not patched["card_renders_markdown"]("> quote")
