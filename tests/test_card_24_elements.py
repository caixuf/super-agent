"""Card 2.4 ('card_components' v2) accepts only plain_text and table among these tags.

The live failure was ``230099 … 200861 unsupported tag note``: the card path added a 1.0
``note`` while streaming, and 2.0 ``markdown`` blocks are equally unsupported under v2.
This pins the converter against the helper block the patch installs.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

REPORT = Path(__file__).resolve().parents[1] / "docs/handover/evidence/card-markdown-native-fix-20261027.diff"
ADAPTER_HINT = Path(os.environ.get("HERMES_FEISHU_ADAPTER", "~/.hermes/hermes-agent/plugins/platforms/feishu/adapter.py")).expanduser()


def helpers():
    if not ADAPTER_HINT.is_file():
        import pytest
        pytest.skip("patched adapter not available: %s" % ADAPTER_HINT)
    text = ADAPTER_HINT.read_text(encoding="utf-8")
    start = text.index("_CARD_UNSUPPORTED_RE = ")
    end = text.index("def _coerce_int(")
    namespace = {"re": re, "os": os, "Any": object, "time": time}
    exec(compile(text[start:end], "<card-helpers>", "exec"), namespace)
    return namespace


def test_streaming_frames_use_only_v2_supported_tags():
    namespace = helpers()
    os.environ["HERMES_FEISHU_CARD_24"] = "1"
    elements = namespace["build_card24_elements"](
        "## 结论\n\n| 维度 | 新 | 旧 |\n|---|---|---|\n| 速度 | **快** | 慢 |\n\n后段 `code`。")
    tags = [element["tag"] for element in elements]
    # card JSON 2.0 addresses text as div + text.plain_text; a bare plain_text element is
    # rejected with "type of element is not supported tag: plain_text" (200621).
    assert tags == ["div", "table", "div"], tags
    assert all(element["text"]["tag"] == "plain_text" for element in (elements[0], elements[2]))
    assert all(column["data_type"] == "text" for column in elements[1]["columns"])
    assert "**" not in elements[0]["text"]["content"] and "`" not in elements[2]["text"]["content"]
    assert not elements[0]["text"]["content"].startswith("#")


def test_2_0_path_is_unchanged_when_24_is_off():
    namespace = helpers()
    os.environ.pop("HERMES_FEISHU_CARD_24", None)
    assert namespace["card_components_v2"]() is False
    assert namespace["card_renders_markdown"]("# 标题\n\n| a | b |\n|---|---|\n| 1 | 2 |")
