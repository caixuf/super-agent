"""md_deliver contract: Markdown becomes a Feishu document, and no file is lost on failure.

The engine's network boundaries (``upload_material`` / ``create_task`` / ``poll``) are
faked, so the tests pin this repo's sequencing and the all-or-nothing rule without network.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import time

import pytest

from agentcore import markdown_delivery as md

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "markdown_delivery_plugin", ROOT / "plugins" / "markdown_delivery" / "__init__.py")
assert _spec and _spec.loader
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)

AUTH = "Bearer t-test"


@pytest.fixture()
def source(tmp_path):
    path = tmp_path / "gap-report.md"
    path.write_text("# 缺口盘点\n\n| 项 | 结论 |\n| --- | --- |\n| 断言面 | 未交卷 |\n", encoding="utf-8")
    return path


def test_import_runs_upload_create_poll_in_order(monkeypatch, source):
    calls = []
    monkeypatch.setattr(md, "upload_material",
                        lambda path, **kw: calls.append(("upload", Path(path).name)) or ("file_tok", "md"))
    monkeypatch.setattr(md, "create_task",
                        lambda token, **kw: calls.append(("create", token, kw["extension"], kw["title"])) or "ticket_1")
    monkeypatch.setattr(md, "poll", lambda ticket, **kw: calls.append(("poll", ticket)) or
                        {"document_id": "doc_1", "url": "https://feishu.cn/docx/doc_1"})
    monkeypatch.setattr(md, "share_with_org", lambda *a, **kw: calls.append(("share", a[0])))

    outcome = md.deliver_markdown(source, authorization=AUTH, title="7116756543 缺口盘点")

    assert outcome["state"] == "delivered" and outcome["url"] == "https://feishu.cn/docx/doc_1"
    assert calls == [("upload", "gap-report.md"),
                     ("create", "file_tok", "md", "7116756543 缺口盘点"),
                     ("poll", "ticket_1"), ("share", "doc_1")]


def test_import_task_declares_docx_and_the_root_mount(monkeypatch):
    sent = {}
    monkeypatch.setattr(md, "_call", lambda method, path, **kw: sent.update(method=method, path=path,
                                                                             body=kw["body"]) or {"ticket": "t"})
    md.create_task("file_tok", extension="md", title="报告", parent_token="", authorization=AUTH)
    assert sent["method"] == "POST" and sent["path"] == "/drive/v1/import_tasks"
    assert sent["body"] == {"file_extension": "md", "file_token": "file_tok", "type": "docx",
                            "file_name": "报告", "point": {"mount_type": 1, "mount_key": ""}}


def test_polling_status_keeps_the_ticket(monkeypatch):
    monkeypatch.setattr(md, "_call", lambda *a, **kw: {"result": {"job_status": 2}})
    with pytest.raises(md.DeliveryError) as error:
        md.poll("ticket_7", authorization=AUTH, wait_seconds=0)
    assert error.value.ticket == "ticket_7"


def test_finished_import_returns_the_document_and_truncation_warnings(monkeypatch):
    monkeypatch.setattr(md, "_call", lambda *a, **kw: {"result": {
        "job_status": 0, "token": "doc_2", "url": "https://feishu.cn/docx/doc_2", "extra": ["1001"]}})
    outcome = md.poll("ticket_8", authorization=AUTH, wait_seconds=0)
    assert outcome["document_id"] == "doc_2" and outcome["warnings"] == ["1001"]


def test_failed_import_reports_the_platform_status(monkeypatch):
    monkeypatch.setattr(md, "_call", lambda *a, **kw: {"result": {
        "job_status": 112, "job_error_msg": "format not supported"}})
    with pytest.raises(RuntimeError) as error:
        md.poll("ticket_9", authorization=AUTH, wait_seconds=0)
    assert "112" in str(error.value)


def test_import_extension_must_match_the_real_suffix(tmp_path):
    notes = tmp_path / "notes.markdown"
    notes.write_text("x", encoding="utf-8")
    assert md.import_extension(notes) == "markdown"
    other = tmp_path / "notes.txt"
    other.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        md.import_extension(other)


def test_oversized_markdown_is_refused_before_upload(tmp_path):
    big = tmp_path / "big.md"
    big.write_text("x", encoding="utf-8")
    assert md.check_source(big)[0] == "md"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(md, "MAX_MARKDOWN_BYTES", 0)
        with pytest.raises(ValueError):
            md.check_source(big)


class _Ctx:
    def __init__(self, settings):
        self.settings = settings
        self.tools = {}
        self.hooks = {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs["handler"]

    def register_hook(self, name, fn):
        self.hooks[name] = fn


class _Token:
    def authorization(self):
        return AUTH


def _ctx(tmp_path):
    return _Ctx({"enabled": True, "outbox_root": str(tmp_path), "env_file": str(tmp_path / "x.env")})


def test_delivery_removes_the_source_only_after_the_document_exists(tmp_path):
    ctx = _ctx(tmp_path)
    seen = []
    handlers = plugin._handler(
        lambda path, **kw: seen.append(kw["authorization"]) or
        {"state": "delivered", "url": "https://feishu.cn/docx/d", "document_id": "d"}, {}, ctx, _Token())
    source = tmp_path / "report.md"
    source.write_text("# 报告", encoding="utf-8")

    out = json.loads(handlers["md_deliver"](path=str(source)))

    assert out["ok"] is True and out["message"] == "https://feishu.cn/docx/d"
    assert not source.exists(), "the raw attachment must not duplicate the document"
    assert seen == [AUTH]


def test_failed_import_keeps_the_file_and_reports_pending(tmp_path):
    ctx = _ctx(tmp_path)

    def explode(path, **kw):
        raise md.DeliveryError("import still running", ticket="ticket_3")

    handlers = plugin._handler(explode, {}, ctx, _Token())
    source = tmp_path / "report.md"
    source.write_text("# 报告", encoding="utf-8")

    out = json.loads(handlers["md_deliver"](path=str(source)))

    assert out["ok"] is False and out["state"] == "pending" and out["ticket"] == "ticket_3"
    assert source.exists(), "no document means the file (and its marker) must survive"


def test_paths_outside_the_outbox_and_non_markdown_are_refused(tmp_path):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    ctx = _ctx(outbox)
    attempted = []
    handlers = plugin._handler(lambda path, **kw: attempted.append(path) or {"state": "delivered"},
                               {}, ctx, _Token())
    outside = tmp_path / "secret.md"
    outside.write_text("x", encoding="utf-8")

    assert json.loads(handlers["md_deliver"](path=str(outside)))["ok"] is False
    assert json.loads(handlers["md_deliver"](path="/tmp/../etc/passwd"))["ok"] is False
    assert attempted == []


def test_non_markdown_inside_the_outbox_is_reported_by_the_engine(tmp_path):
    ctx = _ctx(tmp_path)
    text_file = tmp_path / "report.txt"
    text_file.write_text("x", encoding="utf-8")
    handlers = plugin._handler(lambda path, **kw: md.check_source(path) and {"state": "delivered"},
                               {}, ctx, _Token())

    out = json.loads(handlers["md_deliver"](path=str(text_file)))

    assert out["ok"] is False and "ValueError" in out["message"]


def test_resolve_finishes_a_pending_import_and_cleans_the_source(tmp_path):
    ctx = _ctx(tmp_path)
    source = tmp_path / "report.md"
    source.write_text("# 报告", encoding="utf-8")
    handlers = plugin._handler({}, lambda ticket, **kw: {
        "state": "delivered", "url": "https://feishu.cn/docx/d3", "document_id": "d3"}, ctx, _Token())

    out = json.loads(handlers["md_resolve"](ticket="ticket_4", path=str(source)))

    assert out["ok"] is True and out["message"] == "https://feishu.cn/docx/d3"
    assert not source.exists()


def test_credentials_come_from_the_profile_env_and_are_cached():
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8") as fh:
        fh.write("FEISHU_APP_ID=cli_x\nFEISHU_APP_SECRET='secret-value'\n")
        env_file = fh.name
    assert plugin._env_value(env_file, "FEISHU_APP_ID") == "cli_x"
    assert plugin._env_value(env_file, "FEISHU_APP_SECRET") == "secret-value"
    assert plugin._env_value(env_file, "FEISHU_APP_TOKEN") is None
    token = plugin._TenantToken(_Ctx({"env_file": env_file}))
    token._value, token._expires_at = "cached", time.time() + 600
    assert token.authorization() == "Bearer cached", "a live token must not be refetched"


def test_handlers_accept_the_positional_argument_dict(tmp_path):
    """Hermes dispatches plugin tools as ``handler(args_dict)``; keyword-only handlers fail live."""
    ctx = _ctx(tmp_path)
    handlers = plugin._handler(
        lambda path, **kw: {"state": "delivered", "url": "https://x/docx/pos", "document_id": "pos"},
        lambda ticket, **kw: {"state": "delivered", "url": "https://x/docx/pos2", "document_id": "pos2"}, ctx,
        _Token())
    source = tmp_path / "positional.md"
    source.write_text("# x", encoding="utf-8")

    out = json.loads(handlers["md_deliver"]({"path": str(source)}))

    assert out["ok"] is True and out["message"] == "https://x/docx/pos"
    assert not source.exists()


def test_guidance_only_appears_for_document_requests():
    assert plugin._guide(user_message="查一下这个端口是多少") is None
    guidance = plugin._guide(user_message="把结论整理成文档给我")
    assert "md_deliver" in guidance["context"]
    assert "##FILE:" in guidance["context"]


def test_upload_rejection_surfaces_the_platform_code_and_message(monkeypatch, tmp_path):
    """A 400 from the upload step is either a missing scope or a bad extension; say which."""
    import io
    import urllib.error

    path = tmp_path / "report.md"
    path.write_text("# x", encoding="utf-8")
    body = json.dumps({"code": 99991672, "msg": "Access denied. One of the following scopes is required"}).encode()

    class _Opener:
        def open(self, request, timeout=0):
            raise urllib.error.HTTPError("https://x/upload", 400, "Bad Request", {}, io.BytesIO(body))

    monkeypatch.setattr(md, "_opener", lambda: _Opener())
    with pytest.raises(RuntimeError) as error:
        md.upload_material(path, authorization=AUTH)

    message = str(error.value)
    assert "99991672" in message and "scopes" in message
    assert "Bearer" not in message


def test_document_is_shared_with_the_org_before_reporting_delivered(monkeypatch, source):
    """A bot-owned document is invisible to the requester until the link is shared."""
    calls = []
    monkeypatch.setattr(md, "upload_material", lambda path, **kw: ("file_tok", "md"))
    monkeypatch.setattr(md, "create_task", lambda token, **kw: "ticket_9")
    monkeypatch.setattr(md, "poll", lambda ticket, **kw: {"document_id": "doc_9",
                                                          "url": "https://x/docx/doc_9"})
    monkeypatch.setattr(md, "_call", lambda method, path, **kw: calls.append((method, path, kw.get("params")))
                        or {"permission_public": {"link_share_entity": "tenant_readable"}})

    outcome = md.deliver_markdown(source, authorization=AUTH)

    assert outcome["state"] == "delivered"
    assert calls == [("PATCH", "/drive/v2/permissions/doc_9/public", {"type": "docx"})]


def test_unshared_document_is_not_reported_as_delivered(monkeypatch, source):
    monkeypatch.setattr(md, "upload_material", lambda path, **kw: ("file_tok", "md"))
    monkeypatch.setattr(md, "create_task", lambda token, **kw: "ticket_10")
    monkeypatch.setattr(md, "poll", lambda ticket, **kw: {"document_id": "doc_10",
                                                          "url": "https://x/docx/doc_10"})
    monkeypatch.setattr(md, "_call", lambda *a, **kw: {"permission_public": {"link_share_entity": "closed"}})

    with pytest.raises(RuntimeError) as error:
        md.deliver_markdown(source, authorization=AUTH)
    assert "document share rejected" in str(error.value)


def test_unshared_document_with_fallback_reports_warning(monkeypatch, source):
    monkeypatch.setattr(md, "upload_material", lambda path, **kw: ("file_tok", "md"))
    monkeypatch.setattr(md, "create_task", lambda token, **kw: "ticket_10")
    monkeypatch.setattr(md, "poll", lambda ticket, **kw: {"document_id": "doc_10",
                                                          "url": "https://x/docx/doc_10"})
    monkeypatch.setattr(md, "_call", lambda *a, **kw: {"permission_public": {"link_share_entity": "closed"}})

    outcome = md.deliver_markdown(source, authorization=AUTH, allow_unshared_fallback=True)
    assert outcome["state"] == "delivered"
    assert outcome["document_id"] == "doc_10"
    assert any("document share rejected" in w for w in outcome.get("warnings", []))


def test_resolve_also_shares_the_document_before_reporting_delivered(monkeypatch):
    """A pending import finished on the next turn must not hand out a closed link."""
    calls = []
    monkeypatch.setattr(md, "poll", lambda ticket, **kw: {"document_id": "doc_11",
                                                          "url": "https://x/docx/doc_11"})
    monkeypatch.setattr(md, "_call", lambda method, path, **kw: calls.append((method, path))
                        or {"permission_public": {"link_share_entity": "tenant_readable"}})

    outcome = md.resolve_import("ticket_11", authorization=AUTH)

    assert outcome["state"] == "delivered"
    assert calls == [("PATCH", "/drive/v2/permissions/doc_11/public")]
