"""Final-only HFC projection: pure unit contract plus opt-in real-package checks."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
FINAL_RENDER = ROOT / "integrations/hfc/final_render.py"
SHARED_MARKDOWN = ROOT / "agentcore/card_markdown.py"


@dataclass(frozen=True)
class _Inspection:
    json_bytes: int = 0
    element_count: int = 0
    table_count: int = 0
    violations: tuple[str, ...] = ()

    @property
    def safe(self) -> bool:
        return not self.violations


@dataclass(frozen=True)
class _RenderResult:
    card: dict
    disposition: str
    inspection: _Inspection
    table_overflow: object = field(default_factory=SimpleNamespace)
    limit_reason: str = ""
    primary_text: str = ""


@dataclass
class _Session:
    status: str = "completed"
    delivery_kind: str = "chat"
    answer_text: str = ""
    active_interaction: object | None = None
    execution_scope: str = ""
    notice_kind: str = ""
    notice_title: str = ""
    terminal_disposition: str = ""
    terminal_handoff_record: object | None = None


def _load_units(monkeypatch):
    package_name = "_hfc_final_render_units"
    package = ModuleType(package_name)
    package.__path__ = []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)

    limits = ModuleType(f"{package_name}.card_limits")

    def inspect_card_limits(card):
        data = json.dumps(card, ensure_ascii=False).encode("utf-8")
        return _Inspection(json_bytes=len(data))

    limits.inspect_card_limits = inspect_card_limits
    monkeypatch.setitem(sys.modules, limits.__name__, limits)

    render = ModuleType(f"{package_name}.render")
    render.CardRenderResult = _RenderResult
    render.MAIN_CONTENT_CHUNK_CHARS = 2400
    monkeypatch.setitem(sys.modules, render.__name__, render)

    text = ModuleType(f"{package_name}.text")
    text.normalize_stream_text = lambda value: value or ""
    text.split_markdown_blocks = lambda value, _size: [value]
    monkeypatch.setitem(sys.modules, text.__name__, text)

    shared_spec = importlib.util.spec_from_file_location(
        f"{package_name}.shared_card_markdown", SHARED_MARKDOWN,
    )
    shared = importlib.util.module_from_spec(shared_spec)
    assert shared_spec.loader is not None
    shared_spec.loader.exec_module(shared)
    monkeypatch.setitem(sys.modules, shared.__name__, shared)

    final_spec = importlib.util.spec_from_file_location(
        f"{package_name}.shared_final_render", FINAL_RENDER,
    )
    final = importlib.util.module_from_spec(final_spec)
    assert final_spec.loader is not None
    final_spec.loader.exec_module(final)
    monkeypatch.setitem(sys.modules, final.__name__, final)
    return final, limits


@pytest.fixture
def units(monkeypatch):
    return _load_units(monkeypatch)


def _card(*, main="old", extra=None, schema="2.0"):
    elements = [
        {"tag": "markdown", "element_id": "main_content", "content": main},
        {"tag": "hr", "element_id": "main_divider"},
        {"tag": "markdown", "element_id": "tool_summary", "content": "tool evidence"},
        {"tag": "button", "element_id": "interaction_control",
         "text": {"tag": "plain_text", "content": "Continue"},
         "value": {"callback_token": "opaque"}},
        {"tag": "markdown", "element_id": "footer", "content": "已完成 · model"},
    ]
    if extra is not None:
        elements = extra
    return {
        "schema": schema,
        "config": {
            "update_multi": True,
            "width_mode": "default",
            "style": {"text_size": {"hfc_footer": {"default": "small"}}},
        },
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "Runtime title"},
            "subtitle": {"tag": "plain_text", "content": "tool running"},
        },
        "body": {"elements": elements},
    }


def _result(card=None, *, disposition="card"):
    return _RenderResult(card=copy.deepcopy(card or _card()), disposition=disposition,
                         inspection=_Inspection(), primary_text="old")


def test_opt_out_and_running_are_identity_noops(units):
    final, _ = units
    card = _card()
    answer = _Session(answer_text="new answer")
    completed = _result(card)
    assert final.project_final_result(completed, answer, title="T") is completed

    running = _Session(status="running", answer_text="partial")
    live_result = _result(card)
    assert final.project_final_result(live_result, running, title="T", enabled=True) is live_result


def test_success_uses_shared_tables_code_and_plain_green_header(units):
    final, _ = units
    answer = (
        "## Result\n\nReadable **body** and `a|b`.\n\n"
        "| Item | Value |\n|---|---|\n| a\\|b | **PASS** |\n\n"
        "```cpp\nint answer = 42;\n```\n"
    )
    result = _result()
    session = _Session(answer_text=answer)
    original_session = copy.deepcopy(session)

    original_card = copy.deepcopy(result.card)
    projected = final.project_final_result(result, session, title="Final title", enabled=True)

    assert projected is not result
    assert result.card == original_card
    assert session == original_session
    assert projected.disposition == "card"
    assert projected.primary_text == answer
    assert projected.inspection.safe
    assert projected.card["schema"] == "2.0"
    assert projected.card["config"] == {**original_card["config"], "width_mode": "fill"}
    assert original_card["header"] == {
        "template": "blue",
        "title": {"tag": "plain_text", "content": "Runtime title"},
        "subtitle": {"tag": "plain_text", "content": "tool running"},
    }
    # The final-only presentation intentionally replaces the live header.
    assert projected.card["header"] == {
        "template": "green",
        "title": {"tag": "plain_text", "content": "Final title"},
    }
    elements = projected.card["body"]["elements"]
    assert [element.get("element_id") for element in elements[:3]] == [
        "main_content", "main_content_1", "main_content_2",
    ]
    assert elements[1]["tag"] == "table"
    assert elements[1]["rows"] == [{"c0": "a|b", "c1": "**PASS**"}]
    assert elements[2]["tag"] == "markdown"
    assert "```cpp\nint answer = 42;\n```" in elements[2]["content"]
    assert all(element["text_size"] == "normal"
               for element in elements[:3] if element["tag"] == "markdown")
    assert elements[3:] == original_card["body"]["elements"][1:]


def test_footer_timeline_and_controls_are_preserved_and_original_is_immutable(units):
    final, _ = units
    original_card = _card(main="old")
    result = _result(original_card)
    original_result = copy.deepcopy(result)
    session = _Session(answer_text="replacement")

    projected = final.project_final_result(result, session, title="Final", enabled=True)

    assert result == original_result
    old_elements = original_result.card["body"]["elements"]
    new_elements = projected.card["body"]["elements"]
    assert new_elements[1:] == old_elements[1:]
    assert new_elements[1]["element_id"] == "main_divider"
    assert new_elements[2]["element_id"] == "tool_summary"
    assert new_elements[3]["element_id"] == "interaction_control"
    assert new_elements[3]["value"] == {"callback_token": "opaque"}
    assert new_elements[4]["element_id"] == "footer"


@pytest.mark.parametrize("session", [
    _Session(status="running", answer_text="answer"),
    _Session(status="failed", answer_text="answer"),
    _Session(status="completed", delivery_kind="notice", answer_text="answer"),
    _Session(status="completed", delivery_kind="cron", answer_text="answer"),
    _Session(status="completed", execution_scope="cron", answer_text="answer"),
    _Session(status="completed", notice_title="notice", answer_text="answer"),
    _Session(status="completed", answer_text="answer", active_interaction=object()),
    _Session(status="completed", answer_text="   "),
    _Session(status="completed", answer_text="answer", terminal_disposition="native"),
    _Session(status="completed", answer_text="answer", terminal_handoff_record=object()),
])
def test_pending_legacy_notice_cron_and_empty_answers_are_excluded(units, session):
    final, _ = units
    result = _result()
    assert final.project_final_result(result, session, title="T", enabled=True) is result


def test_non_card_disposition_and_legacy_schema_are_excluded(units):
    final, _ = units
    session = _Session(answer_text="answer")
    for result in (
        _result(disposition="native"),
        _result(disposition="deferred_native"),
        _result(_card(schema="1.0")),
    ):
        assert final.project_final_result(result, session, title="T", enabled=True) is result


def test_crlf_and_code_selector_are_preserved_exactly(units):
    final, _ = units
    answer = "## Result\r\n\r\n```cpp\r\nint value = 1;\r\n```\r\n"
    result = _result()
    projected = final.project_final_result(
        result, _Session(answer_text=answer), title="T", enabled=True,
    )
    assert projected is not result
    assert projected.card["body"]["elements"][0]["content"] == answer


def test_generated_at_markup_is_never_replaced_or_emitted(units):
    final, _ = units
    main = '<at id="ou_verified"></at>\n\nanswer'
    result = _result(_card(main=main))
    assert final.project_final_result(
        result, _Session(answer_text="answer"), title="T", enabled=True,
    ) is result


def test_unsupported_markdown_is_not_projected(units):
    final, _ = units
    six_tables = "\n\n".join(["| a | b |\n|---|---|\n| 1 | 2 |"] * 6)
    result = _result()
    assert final.project_final_result(
        result, _Session(answer_text=six_tables), title="T", enabled=True,
    ) is result
    assert final.project_final_result(
        result, _Session(answer_text='<at id="ou_raw"></at>\n\nanswer'),
        title="T", enabled=True,
    ) is result


def test_oversize_or_unsafe_projection_returns_original_without_truncation(units, monkeypatch):
    final, limits_module = units
    answer = "x" * 29_000
    result = _result()
    monkeypatch.setattr(
        final, "inspect_card_limits",
        lambda _card: _Inspection(json_bytes=29_000, violations=("json_bytes",)),
    )
    assert final.project_final_result(
        result, _Session(answer_text=answer), title="T", enabled=True,
    ) is result
    assert result.card["body"]["elements"][0]["content"] == "old"


def test_malformed_card_or_title_falls_back(units):
    final, _ = units
    session = _Session(answer_text="answer")
    malformed = _result()
    malformed.card["body"] = {"elements": "not-a-list"}
    assert final.project_final_result(malformed, session, title="T", enabled=True) is malformed
    safe = _result()
    assert final.project_final_result(safe, session, title=" ", enabled=True) is safe


@pytest.fixture(scope="module")
def hfc_renderer(tmp_path_factory):
    root = os.environ.get("HFC_SOURCE_ROOT")
    if not root:
        pytest.skip("opt-in: set HFC_SOURCE_ROOT to a real HFC package root")
    source = Path(root).resolve()
    if not (source / "hermes_feishu_card" / "render.py").is_file():
        pytest.fail("HFC_SOURCE_ROOT must contain hermes_feishu_card/render.py")
    private_home = tmp_path_factory.mktemp("hfc-home")
    old_home = {name: os.environ.get(name) for name in (
        "HOME", "HERMES_HOME", "HERMES_FEISHU_CARD_STATE_DIR",
        "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
    )}
    for name in old_home:
        os.environ[name] = str(private_home)
    old_hfc = {name: os.environ.pop(name) for name in tuple(os.environ)
               if name.startswith(("HERMES_FEISHU_CARD_", "HFC_"))}
    sys.path.insert(0, str(source))
    try:
        render_module = importlib.import_module("hermes_feishu_card.render")
        session_module = importlib.import_module("hermes_feishu_card.session")
        if Path(render_module.__file__).resolve().parent.parent != source:
            pytest.fail("HFC import did not resolve to HFC_SOURCE_ROOT")
        shared_spec = importlib.util.spec_from_file_location(
            "hermes_feishu_card.shared_card_markdown", SHARED_MARKDOWN,
        )
        assert shared_spec is not None and shared_spec.loader is not None
        shared = importlib.util.module_from_spec(shared_spec)
        sys.modules[shared_spec.name] = shared
        shared_spec.loader.exec_module(shared)
        spec = importlib.util.spec_from_file_location(
            "hermes_feishu_card.shared_final_render", FINAL_RENDER,
        )
        assert spec is not None and spec.loader is not None
        final = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = final
        spec.loader.exec_module(final)
        yield final, render_module, session_module
    finally:
        sys.modules.pop("hermes_feishu_card.shared_final_render", None)
        sys.modules.pop("hermes_feishu_card.shared_card_markdown", None)
        sys.path.remove(str(source))
        for name, value in old_home.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        os.environ.update(old_hfc)


def test_real_hfc_task_final_keeps_code_language_footer_and_timeline(hfc_renderer):
    final, render_module, session_module = hfc_renderer
    answer = (
        "## 结论\r\n\r\n`a|b` 保留。\r\n\r\n"
        "| 项目 | 结果 |\r\n|---|---|\r\n| x | **通过** |\r\n\r\n"
        "```cpp\r\nint value = 1;\r\n```\r\n"
    )
    session = session_module.CardSession(
        conversation_id="conversation", message_id="message", chat_id="chat",
        status="completed", answer_text=answer, delivery_kind="chat",
    )
    session.timeline.record_tool("tool-1", "Read", "completed", "path")
    result = render_module.render_card_result(
        session, title="Runtime title", presentation="task",
        native_tables=False, timeline_expanded=True, show_reasoning=True,
    )
    assert result.disposition == "card"
    original_card = copy.deepcopy(result.card)
    original_answer = session.answer_text

    projected = final.project_final_result(result, session, title="Final title", enabled=True)

    assert projected is not result
    assert result.card == original_card
    assert session.answer_text == original_answer
    assert projected.card["header"] == {
        "template": "green",
        "title": {"tag": "plain_text", "content": "Final title"},
    }
    elements = projected.card["body"]["elements"]
    main = [element for element in elements
            if str(element.get("element_id", "")).startswith("main_content")]
    assert any(element.get("tag") == "table" for element in main)
    code = "\n".join(element.get("content", "") for element in main
                     if element.get("tag") == "markdown")
    assert "```cpp\r\nint value = 1;\r\n```" in code
    assert "```plain_text" not in code
    original_aux = [element for element in original_card["body"]["elements"]
                    if element.get("element_id") in {"footer", "auxiliary_timeline"}]
    projected_aux = [element for element in elements
                     if element.get("element_id") in {"footer", "auxiliary_timeline"}]
    assert projected_aux == original_aux


def test_real_hfc_classic_final_keeps_footer_and_collapsed_timeline(hfc_renderer):
    final, render_module, session_module = hfc_renderer
    answer = "## Result\n\nFinal answer.\n\n```cpp\nint value = 1;\n```\n"
    session = session_module.CardSession(
        conversation_id="conversation", message_id="message", chat_id="chat",
        status="completed", answer_text=answer, delivery_kind="chat",
    )
    session.timeline.record_tool("tool-1", "Read", "completed", "path")
    result = render_module.render_card_result(
        session, title="Runtime title", presentation="classic",
        timeline_expanded=False, show_reasoning=True,
    )
    original_card = copy.deepcopy(result.card)

    projected = final.project_final_result(result, session, title="Final", enabled=True)

    original_by_id = {
        element.get("element_id"): element
        for element in original_card["body"]["elements"]
        if element.get("element_id") not in {"main_content", "main_content_1", "main_content_2"}
    }
    projected_by_id = {
        element.get("element_id"): element
        for element in projected.card["body"]["elements"]
        if element.get("element_id") not in {"main_content", "main_content_1", "main_content_2"}
    }
    assert "footer" in original_by_id
    assert "auxiliary_timeline" in original_by_id
    assert original_by_id["auxiliary_timeline"]["expanded"] is False
    assert projected_by_id == original_by_id
