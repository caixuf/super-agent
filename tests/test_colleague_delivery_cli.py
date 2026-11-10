"""The host drain that puts a finished task's receipt into a real chat.

The platform is replaced by a stand-in server; the point here is that the drain
takes a queue receipt all the way to an acknowledgement, refuses to run without
credentials, and never uploads files it is only able to describe.
"""
import io
import json
import os
import uuid

import pytest

from agentcore import colleague_delivery_cli
from agentcore.colleague_delivery_cli import MAX_TEXT, _TextOnly, build, main, render
from agentcore.pool import Pool

CHAT = "oc_49c195f21a3979c53edeba1c33e34ed5"
ENV = {"FEISHU_APP_ID": "cli_test_app", "FEISHU_APP_SECRET": "secret"}


class Server:
    def __init__(self):
        self.sent = []
        self.messages = {}

    def handle(self, request):
        if request.full_url.endswith("/tenant_access_token/internal"):
            return {"code": 0, "tenant_access_token": "t", "expire": 7200}
        if request.get_method() != "POST":
            message_id = request.full_url.rsplit("/", 1)[-1].split("?")[0]
            if message_id not in self.messages:
                return {"code": 230001}
            return {"code": 0, "data": {"items": [self.messages[message_id]]}}
        body = json.loads(request.data.decode())
        parsed_content = json.loads(body["content"])
        self.sent.append(body)
        message_id = "om_%d" % len(self.sent)
        if body.get("msg_type") == "interactive":
            self.messages[message_id] = {
                "message_id": message_id, "chat_id": body["receive_id"], "deleted": False,
                "body": {"content": body["content"]}}
        else:
            text = parsed_content["text"]
            self.messages[message_id] = {
                "message_id": message_id, "chat_id": body["receive_id"], "deleted": False,
                "body": {"content": json.dumps({"text": text}, ensure_ascii=False)}}
        return {"code": 0, "data": {"message_id": message_id}}


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class Opener:
    def __init__(self, server):
        self.server = server

    def open(self, request, timeout=None):
        return Response(json.dumps(self.server.handle(request)).encode())


@pytest.fixture
def server():
    return Server()


@pytest.fixture
def drain(monkeypatch, server):
    """Run ``main`` as the host would, with the platform replaced by the stand-in."""
    original = colleague_delivery_cli.from_env

    def run(root):
        output = io.StringIO()
        real = os.environ.copy()
        os.environ.update(ENV)
        monkeypatch.setattr(
            colleague_delivery_cli, "from_env",
            lambda journal, env=None, opener=None: original(journal, env or dict(ENV), Opener(server)))
        try:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr("sys.stdout", output)
                code = main(["--root", str(root), "--profile", "team"])
        finally:
            os.environ.clear()
            os.environ.update(real)
        return code, json.loads(output.getvalue())
    return run


def _queue(root, *, summary="all green", artifacts=None):
    pool = Pool(str(root))
    job = pool.submit("k-" + uuid.uuid4().hex, {"goal": "work"},
                      origin={"profile": "team", "session": "owner", "route": "gateway",
                              "channel": "feishu", "chat": CHAT})
    claim, fd = pool.claim()
    result = {"summary": summary}
    if artifacts:
        result["artifacts"] = artifacts
    pool.finish(job, claim["attempt"], True, result)
    os.close(fd)
    return pool, job


def test_a_finished_task_reaches_the_chat_and_the_receipt_is_acknowledged(tmp_path, drain, server):
    pool, job = _queue(tmp_path)
    code, outcomes = drain(tmp_path)
    assert code == 0 and outcomes["outcomes"][0]["status"] == "delivered"
    assert outcomes["outcomes"][0]["task_id"] == job
    assert len(server.sent) == 1 and server.sent[0]["receive_id"] == CHAT
    assert json.loads(server.sent[0]["content"])["text"].endswith("all green")
    with pool.db() as db:
        row = db.execute("SELECT acknowledged FROM outbox WHERE job=?", (job,)).fetchone()
    assert row["acknowledged"] == 1


def test_a_second_drain_finds_nothing_and_sends_nothing(tmp_path, drain, server):
    _queue(tmp_path)
    assert drain(tmp_path)[1]["outcomes"][0]["status"] == "delivered"
    code, again = drain(tmp_path)
    assert code == 0 and again["outcomes"][0]["status"] == "empty"
    assert len(server.sent) == 1


def test_the_drain_refuses_to_run_without_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    assert main(["--root", str(tmp_path), "--profile", "team"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "feishu_credentials_missing"


def test_build_respects_explicit_attachment_environment(tmp_path, server):
    from agentcore.colleague_delivery_cli import DeliverableTransport
    _, transport = build(tmp_path, profile='team',
                         env=dict(ENV, FEISHU_DELIVER_ATTACHMENTS='1'), opener=Opener(server))
    assert isinstance(transport, DeliverableTransport)


def test_files_are_described_in_the_body_and_never_uploaded(tmp_path, drain, server):
    path = tmp_path / "report.txt"
    path.write_text("detail", encoding="utf-8")
    _queue(tmp_path, artifacts=[str(path)])
    code, outcomes = drain(tmp_path)
    assert code == 0 and outcomes["outcomes"][0]["status"] == "delivered"
    text = json.loads(server.sent[0]["content"])["text"]
    assert str(path) not in text and path.name in text and "未上传" in text


def test_render_states_the_colleague_the_truncation_and_the_files():
    text = render({"task_id": "t1", "status": "completed", "summary": "s",
                   "colleague_label": "评审", "truncated": True,
                   "artifact_paths": ["/tmp/report.txt"]})
    assert text.startswith("【评审】") and "已截断" in text and "/tmp/report.txt" in text


def test_render_is_bounded_for_a_huge_summary():
    assert len(render({"task_id": "t", "status": "completed",
                       "summary": "x" * (MAX_TEXT * 2)})) == MAX_TEXT


def test_text_only_adapter_keeps_artifacts_out_of_the_delivered_message():
    seen = []

    class Capture:
        def deliver(self, address, message, *, idempotency_key):
            seen.append(message)
            return {"accepted": True, "event_id": idempotency_key, "receipt_id": "r"}

    _TextOnly(Capture()).deliver({"route": "r"}, {"task_id": "t", "artifacts": ["/tmp/a"]},
                                 idempotency_key="t")
    assert seen[0]["artifacts"] == [] and seen[0]["artifact_paths"] == ["a"]


def test_text_only_redacts_summary_and_keeps_retry_stable(tmp_path):
    seen = []
    class Capture:
        def deliver(self, address, message, *, idempotency_key):
            seen.append(message)
            return {'accepted': True, 'event_id': idempotency_key, 'receipt_id': 'r'}
    file = tmp_path / '报告.txt'
    file.write_text('private')
    message = {'task_id': 't', 'summary': f'结果在 {file}', 'artifacts': [str(file)]}
    adapter = _TextOnly(Capture())
    adapter.deliver({}, message, idempotency_key='t')
    file.unlink()
    adapter.deliver({}, message, idempotency_key='t')
    assert seen[0] == seen[1]
    assert str(file) not in seen[0]['summary']
    assert seen[0]['artifact_paths'] == ['报告.txt']


def test_deliverable_transport_passes_attempt_artifacts_to_durable_transport(tmp_path):
    from agentcore.colleague_delivery_cli import DeliverableTransport

    attempt_id = 'a' * 32
    attempt_dir = tmp_path / attempt_id
    attempt_dir.mkdir()
    html_file = attempt_dir / "review.html"
    html_file.write_text("<h1>Review</h1>")
    png_file = attempt_dir / "arch.png"
    png_file.write_bytes(b"\x89PNG\r\n\x1a\n")

    delivered_messages = []
    class MockCompletion:
        attempt_root = tmp_path
        attempt_provider = staticmethod(lambda job: attempt_id)

        def deliver(self, address, message, *, idempotency_key):
            delivered_messages.append((address, message, idempotency_key))
            return {"accepted": True, "event_id": idempotency_key, "receipt_id": "rec_1"}

    feishu_actions = []
    class MockFeishuTransport:
        def upload_image(self, path):
            feishu_actions.append(("upload_image", path))
            return "img_key_test"

        def send_image(self, *, origin, delivery_key, image_key):
            feishu_actions.append(("send_image", origin, delivery_key, image_key))

        def upload_file(self, path):
            feishu_actions.append(("upload_file", path))
            return "file_key_test"

        def send_file(self, *, origin, delivery_key, file_key):
            feishu_actions.append(("send_file", origin, delivery_key, file_key))

    transport = DeliverableTransport(MockCompletion(), MockFeishuTransport())

    raw_summary = (
        f"架构审查已完成。\n"
        f"本地HTML文件：{html_file}\n"
        f"架构高清长图：{png_file}\n"
    )

    transport.deliver(
        {"chat": CHAT},
        {"task_id": "t_deliv_1", "summary": raw_summary, "status": "completed"},
        idempotency_key="k_deliv_1"
    )

    assert len(delivered_messages) == 1
    sent_msg = delivered_messages[0][1]
    # Local path should be replaced by friendly placeholder
    assert str(html_file) not in sent_msg["summary"]
    assert str(png_file) not in sent_msg["summary"]
    assert "[文件附件候选: review.html]" in sent_msg["summary"]
    assert "[图片附件候选: arch.png]" in sent_msg["summary"]

    # Feishu upload actions should be triggered for both files
    assert sent_msg['artifacts'] == [str(html_file), str(png_file)]
    assert feishu_actions == []  # The durable transport owns the send journal.


def test_attachment_failure_is_fenced_and_not_acknowledged(tmp_path):
    from agentcore.colleague_delivery_cli import DeliverableTransport
    from agentcore.direct_completion import DirectCompletionTransport

    attempt = 'b' * 32
    directory = tmp_path / 'attempts' / attempt
    directory.mkdir(parents=True)
    report = directory / 'report.txt'
    report.write_text('report')
    calls = []

    def sender(address, item, *, idempotency_key):
        calls.append(item['kind'])
        if item['kind'] == 'file':
            return None  # Could have sent: fence it instead of blindly retrying.
        return {'ok': True, 'message_id': 'm1'}

    direct = DirectCompletionTransport(tmp_path / 'direct', profile='team', sender=sender,
        render=render, attempt_root=tmp_path / 'attempts', attempt_provider=lambda _: attempt)
    transport = DeliverableTransport(direct, None)
    address = {'profile': 'team', 'route': 'gateway'}
    message = {'task_id': 't1', 'summary': str(report), 'status': 'completed'}
    assert transport.deliver(address, message, idempotency_key='t1')['deferred'] is True
    report.unlink()  # A retry must reuse the staged plan even after cleanup.
    assert transport.deliver(address, message, idempotency_key='t1')['deferred'] is True
    assert calls == ['text', 'file']


def test_build_respects_card_delivery_and_emits_canonical_cardkit2(tmp_path, server):
    from agentcore.completion_delivery import deliver_pending
    pool, _ = _queue(tmp_path, summary="canonical card report")
    _, transport = build(
        tmp_path,
        profile="team",
        env=dict(ENV, FEISHU_DELIVER_CARD="1"),
        opener=Opener(server),
    )
    outcomes = deliver_pending(pool, transport, limit=5)
    assert outcomes[0]["status"] == "delivered"
    assert len(server.sent) == 1
    assert server.sent[0]["msg_type"] == "interactive"
    card = json.loads(server.sent[0]["content"])
    assert card["schema"] == "2.0"
    assert card["header"]["template"] == "indigo"
    assert card["header"]["icon"]["token"] == "myai_colorful"
    assert card["body"]["elements"][0]["background_style"] == "blue-50"
