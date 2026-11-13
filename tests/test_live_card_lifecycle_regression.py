import ast
import asyncio
import json
import re
import subprocess
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOST = Path.home() / ".hermes/hermes-agent"
REL = "plugins/platforms/feishu/adapter.py"
BASE = "bf0203a794a07aac3ff764972ab9087baa4e4f4e"

@pytest.fixture
def adapter_class(tmp_path):
    base = subprocess.run(["git", "show", f"{BASE}:{REL}"], cwd=HOST, capture_output=True, text=True) if HOST.is_dir() else None
    if base is None or base.returncode:
        pytest.skip("pinned Hermes source unavailable")
    path = tmp_path / REL
    path.parent.mkdir(parents=True)
    path.write_text(base.stdout)
    subprocess.run(["git", "apply", str(ROOT / "patches/feishu_card_replies.patch")], cwd=tmp_path, check=True)
    tree = ast.parse(path.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "FeishuAdapter")
    methods = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in {"_extract_stages_and_clean_text", "send", "edit_message", "on_processing_start", "on_processing_complete", "_feishu_send_with_retry", "_send_raw_message"}]
    helpers = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in {"_feishu_update_with_retry", "_feishu_update_retryable"}]
    module = ast.fix_missing_locations(ast.Module(body=[*helpers, ast.ClassDef(name="Adapter", bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[]))
    ns = {"asyncio": asyncio, "json": json, "time": time, "uuid": uuid, "re": re, "_FEISHU_SEND_ATTEMPTS": 2, "_FEISHU_UPDATE_ATTEMPTS": 3, "_FEISHU_UPDATE_RETRYABLE_STATUS": frozenset({408, 429, 500, 502, 503, 504}), "_FEISHU_REPLY_FALLBACK_CODES": set(), "_POST_CONTENT_INVALID_RE": re.compile("invalid"), "Optional": Optional, "Any": Any,
          "SendResult": SimpleNamespace, "MessageEvent": object, "ProcessingOutcome": SimpleNamespace(FAILURE="failure"), "_MARKDOWN_HINT_RE": re.compile("z"),
          "logger": SimpleNamespace(warning=lambda *args: None)}
    exec(compile(module, str(path), "exec"), ns)
    return ns["Adapter"]


def make_adapter(cls):
    a = cls()
    a._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(patch=object()))))
    a._card_replies = True
    a._live_card_start_locks = {}
    a.MAX_MESSAGE_LENGTH = 10000
    a.format_message = lambda s: s
    a.truncate_message = lambda s, n: [s]
    a._card_message_ids = {"card": time.time()}
    a._is_card_message = lambda mid: mid in a._card_message_ids
    a._build_reply_card = lambda content, **kw: {"content": content, "finalize": kw["finalize"]}
    a._build_patch_message_body = lambda **kw: kw
    a._build_patch_message_request = lambda **kw: kw
    a._finalize_send_result = lambda response, error: SimpleNamespace(success=True, message_id=None)
    a.patches = []
    async def call(fn, request):
        a.patches.append(json.loads(request["request_body"]["content"]))
        return object()
    a._run_blocking = call
    return a


def test_preview_and_final_edit_keep_one_card_and_stop_heartbeat(adapter_class):
    async def run():
        a = make_adapter(adapter_class)
        stop = asyncio.Event()
        async def heartbeat():
            await stop.wait()
        task = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        a._active_live_card_per_chat = {("chat", ""): {"stop_event": stop, "task": task, "message_id": "card", "started_at": time.time()}}
        preview = await a.send("chat", "progress", metadata={"expect_edits": True})
        assert preview.message_id == "card" and not stop.is_set()
        assert a.patches[-1] == {"content": "progress", "finalize": False}
        final = await a.edit_message("chat", "card", "complete", finalize=True)
        assert final.message_id == "card" and task.done()
        assert a.patches[-1] == {"content": "complete", "finalize": True}
        assert not a._active_live_card_per_chat
    asyncio.run(run())


def test_final_send_waits_for_heartbeat_without_cancelling_turn(adapter_class):
    async def run():
        a = make_adapter(adapter_class)
        stop = asyncio.Event()
        task = asyncio.create_task(stop.wait())
        await asyncio.sleep(0)
        a._active_live_card_per_chat = {("chat", ""): {"stop_event": stop, "task": task, "message_id": "card", "started_at": time.time()}}
        result = await a.send("chat", "done")
        assert result.message_id == "card" and task.done()
        assert a.patches == [{"content": "done", "finalize": True}]
        assert not a._active_live_card_per_chat
    asyncio.run(run())


def test_failed_final_patch_does_not_send_a_second_card(adapter_class):
    async def run():
        a = make_adapter(adapter_class)
        a._finalize_send_result = lambda response, error: SimpleNamespace(success=False, error="network unknown")
        stop = asyncio.Event()
        task = asyncio.create_task(stop.wait())
        await asyncio.sleep(0)
        a._active_live_card_per_chat = {("chat", ""): {"stop_event": stop, "task": task, "message_id": "card", "started_at": time.time()}}
        result = await a.send("chat", "done")
        assert not result.success
        assert len(a.patches) == 1
        assert a._active_live_card_per_chat[("chat", "")]["message_id"] == "card"
    asyncio.run(run())


def test_simultaneous_topics_update_only_their_own_card(adapter_class):
    async def run():
        a = make_adapter(adapter_class)
        a._card_message_ids = {"card-a": time.time(), "card-b": time.time()}
        stop_a, stop_b = asyncio.Event(), asyncio.Event()
        task_a, task_b = asyncio.create_task(stop_a.wait()), asyncio.create_task(stop_b.wait())
        await asyncio.sleep(0)
        a._active_live_card_per_chat = {
            ("chat", "topic-a"): {"stop_event": stop_a, "task": task_a, "message_id": "card-a", "started_at": time.time()},
            ("chat", "topic-b"): {"stop_event": stop_b, "task": task_b, "message_id": "card-b", "started_at": time.time()},
        }
        result = await a.send("chat", "topic a result", metadata={"thread_id": "topic-a"})
        assert result.message_id == "card-a"
        assert stop_a.is_set() and not stop_b.is_set()
        assert ("chat", "topic-b") in a._active_live_card_per_chat
        stop_b.set()
        await task_b
    asyncio.run(run())


def test_failed_turn_closes_card_when_reactions_are_disabled(adapter_class):
    async def run():
        a = make_adapter(adapter_class)
        a._reactions_enabled = lambda: False
        stop = asyncio.Event()
        task = asyncio.create_task(stop.wait())
        await asyncio.sleep(0)
        a._active_live_card_per_chat = {("chat", ""): {"stop_event": stop, "task": task, "message_id": "card", "started_at": time.time()}}
        event = SimpleNamespace(message_id="incoming", source=SimpleNamespace(chat_id="chat", thread_id=None))
        await a.on_processing_complete(event, "failure")
        assert a.patches[-1]["finalize"] is True
        assert not a._active_live_card_per_chat
        assert task.done()
    asyncio.run(run())


def test_probe_rejects_old_card_patch_and_accepts_exact_current_patch(tmp_path):
    import os
    base = subprocess.run(["git", "show", f"{BASE}:{REL}"], cwd=HOST, capture_output=True, text=True) if HOST.is_dir() else None
    if base is None or base.returncode:
        pytest.skip("pinned Hermes source unavailable")
    adapter = tmp_path / REL
    adapter.parent.mkdir(parents=True)
    adapter.write_text(base.stdout)
    subprocess.run(["git", "apply", str(ROOT / "patches/feishu_card_replies.patch")], cwd=tmp_path, check=True)
    env = dict(os.environ, HERMES_AGENT_DIR=str(tmp_path))
    command = ["python3", str(ROOT / "patches/apply_feishu_card_replies.py"), "--check"]
    assert subprocess.run(command, env=env, capture_output=True).returncode == 0
    patched = adapter.read_text()
    assert "await hb_task" in patched
    adapter.write_text(patched.replace("await hb_task", "hb_task.cancel()", 1))
    assert subprocess.run(command, env=env, capture_output=True).returncode == 1


def test_followup_keeps_the_existing_card(adapter_class):
    async def run():
        a = make_adapter(adapter_class)
        a._reactions_enabled = lambda: False
        stop = asyncio.Event()
        task = asyncio.create_task(stop.wait())
        await asyncio.sleep(0)
        existing = {"stop_event": stop, "task": task, "message_id": "card", "started_at": time.time()}
        a._active_live_card_per_chat = {("chat", ""): existing}
        event = SimpleNamespace(message_id="followup", metadata={}, source=SimpleNamespace(chat_id="chat", thread_id=None))
        await a.on_processing_start(event)
        assert a._active_live_card_per_chat[("chat", "")] is existing
        assert not stop.is_set()
        stop.set()
        await task
    asyncio.run(run())


def test_concurrent_start_creates_only_one_card(adapter_class):
    async def run():
        a = make_adapter(adapter_class)
        a._reactions_enabled = lambda: False
        a._active_live_card_per_chat = {}
        entered, release = asyncio.Event(), asyncio.Event()
        sent = []
        async def send_card(**kwargs):
            sent.append(kwargs)
            entered.set()
            await release.wait()
            return SimpleNamespace(success=True, message_id="card")
        a._send_reply_card = send_card
        event = SimpleNamespace(message_id="incoming", metadata={}, source=SimpleNamespace(chat_id="chat", thread_id=None))
        first = asyncio.create_task(a.on_processing_start(event))
        await entered.wait()
        second = asyncio.create_task(a.on_processing_start(event))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)
        assert len(sent) == 1
        info = a._active_live_card_per_chat[("chat", "")]
        info["stop_event"].set()
        await info["task"]
    asyncio.run(run())


def test_retry_keeps_same_delivery_uuid_and_respects_caller_uuid(adapter_class):
    async def run():
        a = make_adapter(adapter_class)
        seen = []
        async def raw(**kwargs):
            seen.append(kwargs["metadata"]["_delivery_uuid"])
            if len(seen) == 1:
                raise TimeoutError("ambiguous result")
            return SimpleNamespace(success=True)
        a._send_raw_message = raw
        await a._feishu_send_with_retry(chat_id="chat", msg_type="interactive", payload="{}", reply_to=None, metadata={"_delivery_uuid": "delivery-123"})
        assert seen == ["delivery-123", "delivery-123"]
        seen.clear()
        await a._feishu_send_with_retry(chat_id="chat", msg_type="interactive", payload="{}", reply_to=None, metadata=None)
        assert len(seen) == 2 and seen[0] == seen[1] and seen[0]
    asyncio.run(run())


def test_raw_create_and_reply_use_the_delivery_uuid(adapter_class):
    async def run():
        a = make_adapter(adapter_class)
        a._client.im.v1.message.create = object()
        a._client.im.v1.message.reply = object()
        a._build_reply_message_body = lambda **kw: kw
        a._build_reply_message_request = lambda mid, body: body
        a._build_create_message_body = lambda **kw: kw
        a._build_create_message_request = lambda kind, body: body
        requests = []
        async def call(fn, request):
            requests.append(request)
            return object()
        a._run_blocking = call
        for reply in (None, "incoming"):
            await a._send_raw_message(chat_id="chat", msg_type="interactive", payload="{}", reply_to=reply, metadata={"_delivery_uuid": "fixed-uuid"})
        assert [r["uuid_value"] for r in requests] == ["fixed-uuid", "fixed-uuid"]
    asyncio.run(run())


def test_install_rejects_incompatible_host_before_creating_profile(tmp_path):
    import os
    host = tmp_path / "host"
    adapter = host / REL
    adapter.parent.mkdir(parents=True)
    adapter.write_text("# incompatible adapter\n")
    user_home = tmp_path / "user"
    user_home.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    hermes = bindir / "hermes"
    hermes.write_text("#!/bin/sh\nexit 0\n")
    hermes.chmod(0o755)
    env = dict(os.environ, HOME=str(user_home), PATH=str(bindir) + os.pathsep + os.environ["PATH"], HERMES_AGENT_DIR=str(host))
    env.pop("HERMES_HOME", None)
    proc = subprocess.run(["bash", str(ROOT / "install/install.sh"), "--profile", "fresh", "--skip-deps", "--skip-memory"], env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0
    assert "卡片补丁预检失败" in proc.stderr
    assert not (user_home / ".hermes/profiles/fresh").exists()
    assert adapter.read_text() == "# incompatible adapter\n"


def test_known_card_version_migrates_with_backup_and_is_idempotent(tmp_path, monkeypatch):
    import hashlib
    import importlib.util
    base = subprocess.run(["git", "show", f"{BASE}:{REL}"], cwd=HOST, capture_output=True, text=True) if HOST.is_dir() else None
    if base is None or base.returncode:
        pytest.skip("pinned Hermes source unavailable")
    adapter = tmp_path / REL
    adapter.parent.mkdir(parents=True)
    adapter.write_text(base.stdout)
    template = (ROOT / "patches/legacy/feishu_card_replies_1704d06.patch.template").read_text()
    footer = '        footer_items.append("执行节点: sample-host")'
    subtitle = '                "subtitle": {"tag": "plain_text", "content": "sample subtitle"},'
    previous = tmp_path / "previous.patch"
    previous.write_text(template.replace("__LEGACY_FOOTER_LINE__", footer).replace("__LEGACY_SUBTITLE_LINE__", subtitle))
    subprocess.run(["git", "apply", str(previous)], cwd=tmp_path, check=True)
    old = adapter.read_bytes()
    spec = importlib.util.spec_from_file_location("card_migration_test", ROOT / "patches/apply_feishu_card_replies.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "HERMES_TREE", tmp_path)
    monkeypatch.setattr(module, "_PREVIOUS_ADAPTER_SHA256", hashlib.sha256(old).hexdigest())
    assert module._migration_candidate() is not None
    assert module.apply() == 0
    assert module.check() == 0
    assert module.apply() == 0
    backups = list(adapter.parent.glob("adapter.py.superagent-*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == old
    assert adapter.read_bytes() != old

def test_segment_boundary_keeps_card_open_until_turn_final(adapter_class):
    async def run():
        a = make_adapter(adapter_class)
        stop = asyncio.Event()
        task = asyncio.create_task(stop.wait())
        await asyncio.sleep(0)
        a._active_live_card_per_chat = {("chat", ""): {"stop_event": stop, "task": task, "message_id": "card", "started_at": time.time()}}
        result = await a.edit_message("chat", "card", "segment", finalize=True, metadata={"is_turn_final": False})
        assert result.success and not stop.is_set()
        assert a.patches[-1] == {"content": "segment", "finalize": False}
        assert "card" in a._card_message_ids
        await a.edit_message("chat", "card", "complete", finalize=True, metadata={"is_turn_final": True})
        assert stop.is_set() and task.done()
        assert a.patches[-1]["finalize"] is True
    asyncio.run(run())


def test_private_reasoning_never_reaches_visible_stage_or_content(adapter_class):
    stage, text = adapter_class._extract_stages_and_clean_text("##STAGE: 检查中\n<think>private secret</think>公开结果")
    assert stage == "检查中" and text == "公开结果"
    for content in ("<think>private secret</think>", "<think>unfinished secret", "<think>\n##STAGE: secret\n</think>answer"):
        stage, text = adapter_class._extract_stages_and_clean_text(content)
        assert "secret" not in stage + text



def test_layered_card_overlay_is_replayable_and_exact(tmp_path):
    import os, shutil
    backup=Path.home()/".hermes/superagent/caixuf/backups/card-prototype-20261103-205112"
    patch=ROOT/"patches/feishu_card_lifecycle.patch"
    if not backup.is_dir(): pytest.skip("verified card baseline unavailable")
    files=[line[6:] for line in patch.read_text().splitlines() if line.startswith("--- a/")]
    for f in files:
        dst=tmp_path/f; dst.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(backup/f,dst)
    env=dict(os.environ,HERMES_AGENT_DIR=str(tmp_path))
    overlay=["python3",str(ROOT/"patches/apply_feishu_card_lifecycle.py")]
    base=["python3",str(ROOT/"patches/apply_feishu_card_replies.py")]
    assert subprocess.run(overlay+["--apply"],env=env,capture_output=True).returncode==0
    assert subprocess.run(overlay+["--apply"],env=env,capture_output=True).returncode==0
    assert subprocess.run(base+["--check"],env=env,capture_output=True).returncode==0
    target=tmp_path/REL; target.write_text(target.read_text().replace("sa_card_update_unresolved","tampered_marker"))
    assert subprocess.run(overlay+["--check"],env=env,capture_output=True).returncode!=0
    assert subprocess.run(base+["--check"],env=env,capture_output=True).returncode!=0


def test_final_reply_without_card_ownership_falls_through_instead_of_dropping(tmp_path):
    base = subprocess.run(["git", "show", f"{BASE}:{REL}"], cwd=HOST, capture_output=True, text=True) if HOST.is_dir() else None
    if base is None or base.returncode:
        pytest.skip("pinned Hermes source unavailable")
    path = tmp_path / REL
    path.parent.mkdir(parents=True)
    path.write_text(base.stdout)
    subprocess.run(["git", "apply", str(ROOT / "patches/feishu_card_replies.patch")], cwd=tmp_path, check=True)
    subprocess.run(["git", "apply", "--include", REL, str(ROOT / "patches/feishu_card_lifecycle.patch")], cwd=tmp_path, check=True)
    tree = ast.parse(path.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "FeishuAdapter")
    methods = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in {"send", "_sa_patch_owned_card", "_sa_merge_stage_text", "_extract_stages_and_clean_text", "_send_retry_is_final"}]
    module = ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(name="Adapter", bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[]))
    ns = {
        "asyncio": asyncio, "json": json, "re": re, "Optional": Optional, "Any": Any,
        "SendResult": lambda **kw: SimpleNamespace(**kw),
        "_MARKDOWN_HINT_RE": re.compile("z"),
        "_feishu_update_with_retry": lambda fn, what="": fn(),
        "logger": SimpleNamespace(warning=lambda *args: None),
    }
    exec(compile(module, str(path), "exec"), ns)
    a = ns["Adapter"]()
    a._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(patch=lambda req: None))))
    a._card_replies = True
    a.MAX_MESSAGE_LENGTH = 10000
    a.format_message = lambda s: s
    a.truncate_message = lambda s, n: [s]
    a._card_message_ids = {"card-1": 0.0}
    stop_ev = asyncio.Event()
    a._active_live_card_per_chat = {("chat", "", "sess-1", "turn-1"): {"message_id": "card-1", "started_at": 0.0, "stop_event": stop_ev, "update_lock": asyncio.Lock()}}
    sent_cards = []
    edited_cards = []
    built_cards = []
    async def fake_send_reply_card(**kw):
        sent_cards.append(kw)
        return SimpleNamespace(success=True, message_id="card-new")
    async def fake_edit_message(chat_id, msg_id, content, **kw):
        edited_cards.append(content)
        return SimpleNamespace(success=True, message_id=msg_id)
    a._send_reply_card = fake_send_reply_card
    a.edit_message = fake_edit_message
    a._build_reply_card = lambda text, **kw: built_cards.append((text, kw)) or {"text": text}
    a._build_patch_message_body = lambda **kw: kw
    a._build_patch_message_request = lambda **kw: kw
    a._run_blocking = lambda fn, *args: asyncio.sleep(0)
    a._finalize_send_result = lambda resp, err: SimpleNamespace(success=True)

    async def run():
        # 1. Status update without ownership is suppressed cleanly with error_kind
        res_status = await a.send("chat", "progress", metadata={"_sa_status": True, "_sa_session": "sess-1", "_sa_turn": "turn-other"})
        assert res_status.success is False
        assert res_status.error == "missing card session ownership"
        assert res_status.error_kind == "sa_card_update_unresolved"

        # 2. Final user reply without matching ownership must NOT be dropped; falls through to _send_reply_card
        res_final = await a.send("chat", "final answer", metadata={"_sa_session": "sess-1", "_sa_turn": "turn-other"})
        assert res_final.success is True
        assert res_final.message_id == "card-new"
        assert len(sent_cards) == 1 and sent_cards[0]["content"] == "final answer"

        # 3. Owned _sa_status commentary/tool updates accumulate into ##STAGE: and collapse on final
        await a.send("chat", "正在检索 mff_sync 状态机", metadata={"_sa_status": True, "_sa_session": "sess-1", "_sa_turn": "turn-1"})
        await a.send("chat", "正在运行 pytest", metadata={"_sa_status": True, "_sa_session": "sess-1", "_sa_turn": "turn-1"})
        assert "##STAGE: 正在检索 mff_sync 状态机" in edited_cards[-1]
        assert "##STAGE: 正在运行 pytest" in edited_cards[-1]
        await a.send("chat", "结论：全部通过", metadata={"_sa_session": "sess-1", "_sa_turn": "turn-1"})
        final_text, final_kw = built_cards[-1]
        assert final_kw["finalize"] is True
        assert "##STAGE: 正在检索 mff_sync 状态机" in final_text
        assert "##STAGE: 正在运行 pytest" in final_text
        assert "结论：全部通过" in final_text

    asyncio.run(run())
