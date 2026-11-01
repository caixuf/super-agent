"""End-to-end test of patches/patch_card_markdown_native.py on a synthetic pristine adapter.

The patch is the only thing that puts the card helpers on a host, so it is tested the way it
runs: copy a minimal adapter shaped like the real one, apply, then exercise the helpers and
the emitted card payloads (2.0 markdown/table, 2.4 div/table, 1.0 fallback).
"""
from __future__ import annotations

import importlib.util
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
    """Only the per-card table cap still forces a post.

    Fenced code, block quotes and horizontal rules used to be rejected here on the belief that
    card JSON could not express them. It can: both 1.0 and 2.0 cards were sent to a live chat
    with all three constructs and returned code=0, and 2.0 was confirmed rendering by the owner.
    Rejecting them cost every reply containing a code block or a `---` divider its card.
    """
    assert patched["card_renders_markdown"]("普通 **粗体** 与 `code`")
    assert patched["card_renders_markdown"]("```bash\necho 1\n```")
    assert patched["card_renders_markdown"]("> quote")
    assert patched["card_renders_markdown"]("上面\n\n---\n\n下面")
    # The six-table reply still cannot be a card: Feishu caps tables per card at five.
    assert not patched["card_renders_markdown"](
        "\n\n".join(["| a | b |\n|---|---|\n| 1 | 2 |"] * 6))


def test_fenced_pipe_rows_are_not_lifted_into_tables(patched):
    """A `| a | b |` row inside a fence is snippet text, not a table.

    Awk/shell one-liners commonly contain pipes; splitting them into a table element tore the
    code block apart and left the prose around it mangled.
    """
    snippet = "```bash\nawk -F'|' '{print $2}' f\n| not | a | table |\n|---|---|\n```"
    assert patched["card_renders_markdown"](snippet)
    assert patched["_card_tables"](snippet.splitlines()) == []
    # A real table outside the fence is still detected.
    mixed = snippet + "\n\n| 项 | 值 |\n|---|---|\n| 状态 | ok |\n"
    assert len(patched["_card_tables"](mixed.splitlines())) == 1


# --- 0.22 host shape -------------------------------------------------------------------
#: 0.22.2 has no ``_markdown_needs_post`` and gates the card on one line. These anchors are
#: what a stock 0.22.2 adapter actually contains (copied from the installed file, trimmed to
#: the parts the patch touches); without them the patch refuses to write on 0.22.
ADAPTER_022 = '''"""Minimal stand-in for a 0.22.x host adapter: only the anchors the patch needs."""
from __future__ import annotations

import json
import re
import time
from typing import Any

_MARKDOWN_HINT_RE = re.compile(r"(^\\|.*\\|\\s*\\n\\|[-:|\\s]+\\|)|(^#{1,6}\\s)")
_POST_CONTENT_INVALID_RE = re.compile(r"content format of the post type is incorrect", re.IGNORECASE)


class Adapter:
    _card_replies = True

    def __init__(self, bot_name="bot"):
        self._bot_name = bot_name

    def _is_card_message(self, message_id):
        return True

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        chunks = [content]
        if getattr(self, "_card_replies", False) and (metadata or {}).get("expect_edits"):
            return "card"

    async def edit_message(self, chat_id, message_id, content, *, finalize=False, metadata=None):
        content = content
        turn_final = bool(finalize and (metadata or {}).get("is_turn_final", True))
        if getattr(self, "_card_replies", False) and self._is_card_message(message_id):
            card = self._build_reply_card(
                content, finalize=turn_final, started_at=None)
            return "card", card

    def _build_reply_card(self, content, *, finalize=False, started_at=None):
        name = (str(getattr(self, "_bot_name", "") or "Hermes")).strip() or "Hermes"
        elements: list[dict[str, Any]] = [{"tag": "markdown", "content": content or " "}]
        if not finalize:
            elapsed = max(0, int(time.time() - (started_at or time.time())))
            mins, secs = divmod(elapsed, 60)
            tip = "RUNNING"
            elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": tip}]})
        return {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": name},
                       "template": "green" if finalize else "blue"},
            "elements": elements,
        }
'''


@pytest.fixture()
def patched_022(tmp_path, monkeypatch):
    for key in ("HERMES_FEISHU_CARD_24", "HERMES_FEISHU_CARD_V2"):
        monkeypatch.delenv(key, raising=False)
    tree = tmp_path / "agent022"
    target = tree / "plugins/platforms/feishu/adapter.py"
    target.parent.mkdir(parents=True)
    target.write_text(ADAPTER_022, encoding="utf-8")
    env = dict(os.environ, HERMES_AGENT_DIR=str(tree))
    first = subprocess.run([sys.executable, str(PATCH), "--apply"], env=env,
                           capture_output=True, text=True)
    assert first.returncode == 0, first.stdout + first.stderr
    check = subprocess.run([sys.executable, str(PATCH), "--check"], env=env,
                           capture_output=True, text=True)
    assert check.returncode == 0 and "022" in check.stdout, check.stdout
    again = subprocess.run([sys.executable, str(PATCH), "--apply"], env=env,
                           capture_output=True, text=True)
    assert again.returncode == 0 and "已在位" in again.stdout
    source = target.read_text(encoding="utf-8")
    namespace = {"re": re, "os": os, "Any": object, "time": time}
    exec(compile(source, "<patched-022>", "exec"), namespace)
    return namespace


def test_0_22_host_gets_the_same_2_0_card(patched_022):
    """The 0.22 anchors must emit the payload the pre-0.22 anchors emit."""
    card = _card_for(patched_022, "## 结论\n\n| 维度 | 新 |\n|---|---|\n| 速度 | 快 |\n")
    assert card["schema"] == "2.0"
    assert card["config"]["update_multi"] is True
    assert [element["tag"] for element in card["body"]["elements"]] == ["markdown", "table"]


def test_0_22_host_2_4_mode_uses_div_and_text_tables(patched_022):
    """Same sample as the pristine case: leading prose + table + trailing prose."""
    card = _card_for(patched_022, "## 结论\n\n| a | b |\n|---|---|\n| 1 | 2 |", finalize=False, card24=True)
    assert [element["tag"] for element in card["body"]["elements"]] == ["div", "table", "div"]


def test_0_22_tree_is_not_silently_skipped(tmp_path):
    """An unrecognised tree must be a refusal (exit 2), never a silent 'nothing to do'."""
    spec = importlib.util.spec_from_file_location("_patch", PATCH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.shape_of(PRISTINE) == "pristine"
    assert module.shape_of(ADAPTER_022) == "022"
    assert module.shape_of("def nothing(): pass\n") == "unknown"

    tree = tmp_path / "agent_unknown"
    target = tree / "plugins/platforms/feishu/adapter.py"
    target.parent.mkdir(parents=True)
    target.write_text("def nothing(): pass\n", encoding="utf-8")
    result = subprocess.run([sys.executable, str(PATCH), "--check", "--agent-dir", str(tree)],
                            capture_output=True, text=True)
    assert result.returncode == 2, result.stdout
    assert "认不出" in result.stdout


@pytest.mark.parametrize("source", [PRISTINE, ADAPTER_022], ids=["pristine", "022"])
def test_malformed_tip_is_repaired_despite_present_probe(tmp_path, source):
    spec = importlib.util.spec_from_file_location("_patch_tip", PATCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tree = tmp_path / "agent"
    target = tree / "plugins/platforms/feishu/adapter.py"
    target.parent.mkdir(parents=True)
    target.write_text(source, encoding="utf-8")
    command = [sys.executable, str(PATCH), "--agent-dir", str(tree)]
    first = subprocess.run(command + ["--apply"], capture_output=True, text=True)
    assert first.returncode == 0, first.stdout + first.stderr
    broken = target.read_text().replace(module.NOTE_NEW, module.NOTE_MALFORMED)
    assert broken != target.read_text()
    target.write_text(broken, encoding="utf-8")
    with pytest.raises(IndentationError):
        compile(broken, str(target), "exec")
    check = subprocess.run(command + ["--check"], capture_output=True, text=True)
    assert check.returncode != 0, check.stdout
    repair = subprocess.run(command + ["--apply"], capture_output=True, text=True)
    assert repair.returncode == 0, repair.stdout + repair.stderr
    repaired = target.read_text()
    compile(repaired, str(target), "exec")
    assert module.NOTE_MALFORMED not in repaired
    again = subprocess.run(command + ["--apply"], capture_output=True, text=True)
    assert again.returncode == 0 and "已在位" in again.stdout
    assert target.read_text() == repaired


@pytest.mark.parametrize('already_patched', [False, True])
def test_unknown_syntax_error_is_rejected_without_writing(tmp_path, already_patched):
    tree = tmp_path / 'agent'
    target = tree / 'plugins/platforms/feishu/adapter.py'
    target.parent.mkdir(parents=True)
    target.write_text(ADAPTER_022)
    command = [sys.executable, str(PATCH), '--agent-dir', str(tree)]
    if already_patched:
        result = subprocess.run(command + ['--apply'], capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
    broken = target.read_text() + '\nif True:\n'
    target.write_text(broken)
    for mode in ('--check', '--apply'):
        result = subprocess.run(command + [mode], capture_output=True, text=True)
        assert result.returncode == 2, result.stdout + result.stderr
        assert target.read_text() == broken
