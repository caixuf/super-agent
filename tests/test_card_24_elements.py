"""Card 2.4 ('card_components' v2) accepts only plain_text and table among these tags.

The live failure was ``230099 … 200861 unsupported tag note``: the card path added a 1.0
``note`` while streaming, and 2.0 ``markdown`` blocks are equally unsupported under v2.
This pins the converter against the helper block the patch installs.

Host coupling is deliberate: the pin reads the block out of the *installed* adapter, so it
only means something where the patch actually landed. A host whose adapter carries no patch
(a vanilla Hermes install, a newer upstream adapter) is a normal environment, not a defect
-- hence a reasoned skip, never an error. The environment-independent twin of this pin is
``tests/test_card_markdown_native_patch.py`` (synthetic pristine adapter + in-repo patch),
and ``test_the_pin_still_fails_on_a_broken_converter`` below keeps this file from decaying
into an always-green decoration.
"""
from __future__ import annotations

import importlib.util
import os
import re
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
REPORT = REPO / "docs/handover/evidence/card-markdown-native-fix-20261027.diff"
PATCH = REPO / "patches" / "patch_card_markdown_native.py"
ADAPTER_ENV = "HERMES_FEISHU_ADAPTER"
ADAPTER_HINT = Path("~/.hermes/hermes-agent/plugins/platforms/feishu/adapter.py").expanduser()

#: The block is sliced between these two markers, so both must be present. Their absence is
#: how "this host has an adapter but not this patch" shows up -- an environment fact, not a
#: failure. 2026-10-31: the unguarded slice raised ``ValueError`` instead, which turned
#: ``verify.sh`` red on any machine running an unpatched Hermes.
MARKER_START = "_CARD_UNSUPPORTED_RE = "
MARKER_END = "def _coerce_int("

SAMPLE = "## 结论\n\n| 维度 | 新 | 旧 |\n|---|---|---|\n| 速度 | **快** | 慢 |\n\n后段 `code`。"


def _adapter_path() -> Path:
    """Resolved per call, so the guard tests below can point at a fixture."""
    return Path(os.environ.get(ADAPTER_ENV, str(ADAPTER_HINT))).expanduser()


def _namespace(source: str, origin: str) -> dict:
    namespace = {"re": re, "os": os, "Any": object, "time": time}
    exec(compile(source, origin, "exec"), namespace)
    return namespace


def helpers():
    """The helper block as installed on this host; a reasoned skip when it is not there."""
    adapter = _adapter_path()
    if not adapter.is_file():
        pytest.skip("patched adapter not available: %s" % adapter)
    text = adapter.read_text(encoding="utf-8")
    missing = [marker for marker in (MARKER_START, MARKER_END) if marker not in text]
    if missing:
        pytest.skip("host adapter is not the patched one (missing %s): %s"
                    % (", ".join(repr(marker) for marker in missing), adapter))
    block = text[text.index(MARKER_START):text.index(MARKER_END)]
    try:
        return _namespace(block, "<card-helpers>")
    except NameError as err:
        # Host adapter carries markers but slice refs outer names (env drift).
        pytest.skip(
            "host adapter helper slice incomplete (%s): %s" % (err, adapter)
        )


def _assert_card24_contract(namespace) -> None:
    """The contract that both this pin and its negative control run through.

    card JSON 2.0 addresses text as div + text.plain_text; a bare plain_text element is
    rejected with "type of element is not supported tag: plain_text" (200621), a 1.0
    ``markdown`` tag with "unsupported tag" (230099/200861).
    """
    os.environ["HERMES_FEISHU_CARD_24"] = "1"
    elements = namespace["build_card24_elements"](SAMPLE)
    tags = [element["tag"] for element in elements]
    assert tags == ["div", "table", "div"], tags
    assert all(element["text"]["tag"] == "plain_text" for element in (elements[0], elements[2]))
    assert all(column["data_type"] == "text" for column in elements[1]["columns"])
    assert "**" not in elements[0]["text"]["content"] and "`" not in elements[2]["text"]["content"]
    assert not elements[0]["text"]["content"].startswith("#")


def test_streaming_frames_use_only_v2_supported_tags():
    _assert_card24_contract(helpers())


def test_2_0_path_is_unchanged_when_24_is_off():
    namespace = helpers()
    os.environ.pop("HERMES_FEISHU_CARD_24", None)
    assert namespace["card_components_v2"]() is False
    assert namespace["card_renders_markdown"]("# 标题\n\n| a | b |\n|---|---|\n| 1 | 2 |")


def test_guard_skips_when_the_host_has_no_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv(ADAPTER_ENV, str(tmp_path / "absent.py"))
    with pytest.raises(pytest.skip.Exception) as info:
        helpers()
    assert "patched adapter not available" in str(info.value)


def test_guard_skips_a_host_adapter_without_the_patch(tmp_path, monkeypatch):
    """The 2026-10-31 regression: unpatched adapter present -> ValueError, not a skip."""
    adapter = tmp_path / "adapter.py"
    adapter.write_text("def _coerce_int(value, default=None):\n    return default\n",
                       encoding="utf-8")
    monkeypatch.setenv(ADAPTER_ENV, str(adapter))
    with pytest.raises(pytest.skip.Exception) as info:
        helpers()
    assert "not the patched one" in str(info.value)
    assert MARKER_START.rstrip() in str(info.value)


def _converter_from_the_in_repo_patch(mutate=None):
    """The helper block the patch installs, taken from the patch itself (single source)."""
    spec = importlib.util.spec_from_file_location("_card_markdown_native_patch", PATCH)
    assert spec is not None and spec.loader is not None, PATCH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # its __main__ guard keeps importing side-effect free
    source = module.HELPERS
    if mutate is not None:
        source = mutate(source)
    return _namespace(source, "<in-repo-card-helpers>")


def test_the_pin_still_fails_on_a_broken_converter():
    """Making an unpatched host skip must not disarm the pin: 1.0 tags must still fail."""
    _assert_card24_contract(_converter_from_the_in_repo_patch())
    broken = _converter_from_the_in_repo_patch(
        mutate=lambda source: source.replace('{"tag": "div",', '{"tag": "markdown",'))
    with pytest.raises(AssertionError):
        _assert_card24_contract(broken)