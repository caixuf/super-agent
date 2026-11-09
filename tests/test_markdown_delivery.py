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


def test_share_sets_tenant_editable_and_grants_member_permissions(monkeypatch, source):
    """Verified: public permission sets tenant_editable and members receive full_access/edit."""
    calls = []
    monkeypatch.setattr(md, "upload_material", lambda path, **kw: ("file_tok", "md"))
    monkeypatch.setattr(md, "create_task", lambda token, **kw: "ticket_perm")
    monkeypatch.setattr(md, "poll", lambda ticket, **kw: {"document_id": "doc_perm",
                                                          "url": "https://x/docx/doc_perm"})

    def fake_call(method, path, **kw):
        calls.append((method, path, kw.get("body")))
        if "/public" in path:
            return {"permission_public": {"link_share_entity": kw.get("body", {}).get("link_share_entity")}}
        if "/members" in path:
            return {"member": {"perm": kw.get("body", {}).get("perm")}, "code": 0}
        return {}

    monkeypatch.setattr(md, "_call", fake_call)

    outcome = md.deliver_markdown(
        source, authorization=AUTH,
        chat_id="oc_group123", user_id="ou_user456"
    )

    assert outcome["state"] == "delivered"
    # Step 1: public PATCH with tenant_editable
    assert any(c[0] == "PATCH" and "/public" in c[1] and c[2].get("link_share_entity") == "tenant_editable" for c in calls)
    # Step 2: members POST for openid user with full_access
    assert any(c[0] == "POST" and "/members" in c[1] and c[2].get("member_type") == "openid"
               and c[2].get("member_id") == "ou_user456" and c[2].get("perm") == "full_access" for c in calls)
    # Step 3: members POST for openchat with edit
    assert any(c[0] == "POST" and "/members" in c[1] and c[2].get("member_type") == "openchat"
               and c[2].get("member_id") == "oc_group123" and c[2].get("perm") == "edit" for c in calls)


def test_share_falls_back_to_tenant_readable_when_editable_restricted(monkeypatch):
    """When tenant policy restricts tenant_editable, fallback to tenant_readable succeeds."""
    patch_calls = []

    def fake_call(method, path, **kw):
        if "/public" in path:
            body = kw.get("body", {})
            entity = body.get("link_share_entity")
            patch_calls.append(entity)
            if entity == "tenant_editable":
                # Simulated tenant policy restriction
                return {"permission_public": {"link_share_entity": "closed"}}
            if entity == "tenant_readable":
                return {"permission_public": {"link_share_entity": "tenant_readable"}}
        return {}

    monkeypatch.setattr(md, "_call", fake_call)

    warning = md.share_with_org("doc_fallback", authorization=AUTH)
    assert warning is None
    assert patch_calls == ["tenant_editable", "tenant_readable"]


def test_member_permission_retry_with_edit_on_full_access_failure(monkeypatch):
    """If full_access fails, the engine retries with edit."""
    member_calls = []

    def fake_call(method, path, **kw):
        if "/members" in path:
            perm = kw.get("body", {}).get("perm")
            member_calls.append(perm)
            if perm == "full_access":
                raise RuntimeError("full_access not permitted")
            return {"member": {"perm": "edit"}, "code": 0}
        return {}

    monkeypatch.setattr(md, "_call", fake_call)

    ok, err = md.grant_member_permission("doc_retry", authorization=AUTH, member_id="user_abc", perm="full_access")
    assert ok is True
    assert err is None
    assert member_calls == ["full_access", "edit"]


def test_plugin_deliver_automatically_passes_session_caller_identity(tmp_path, monkeypatch):
    """md_deliver reads HERMES_SESSION_CHAT_ID and USER_ID from context and passes to engine."""
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "oc_caller_chat")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "ou_caller_user")

    delivered_args = {}

    def fake_deliver(source, **kwargs):
        delivered_args.update(kwargs)
        return {"state": "delivered", "url": "https://x/docx/session_doc", "document_id": "session_doc"}

    ctx = _ctx(tmp_path)
    handlers = plugin._handler(fake_deliver, {}, ctx, _Token())
    source = tmp_path / "session_report.md"
    source.write_text("# Test", encoding="utf-8")

    out = json.loads(handlers["md_deliver"](path=str(source)))

    assert out["ok"] is True
    assert delivered_args.get("chat_id") == "oc_caller_chat"
    assert delivered_args.get("user_id") == "ou_caller_user"


def test_routine_inquiries_do_not_trigger_document_guidance():
    """Routine status queries, explanations, and summaries must answer directly without doc guidance."""
    assert plugin._guide(user_message="查一下现在的状态") is None
    assert plugin._guide(user_message="汇报一下当前进展") is None
    assert plugin._guide(user_message="进度如何，有几个挂了") is None
    assert plugin._guide(user_message="请解释一下这个报错的原因") is None

    # Only explicit requests for a document or report trigger guidance
    guided = plugin._guide(user_message="把结论整理成文档给我")
    assert guided is not None
    assert "md_deliver" in guided["context"]
    assert "##FILE:" in guided["context"]
    assert "Routine questions" in guided["context"]

