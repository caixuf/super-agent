"""Unit tests for agentcore.card_stream."""
from __future__ import annotations

import pytest

from agentcore.card_stream import CardStreamBuilder


def test_card_stream_builder_progression():
    clock_time = 1000.0
    builder = CardStreamBuilder(
        task_id="task-stream-001",
        title="自动化研发任务",
        clock=lambda: clock_time,
    )

    # 1. Initial queued state
    card_queued = builder.render_card()
    assert card_queued["header"]["template"] == "grey"
    assert "[排队中]" in card_queued["header"]["title"]["content"]
    assert card_queued["config"]["update_multi"] is True

    # 2. Advance stage 1 to running
    clock_time += 10.0
    builder.advance("analysis", status="running", note="正在解析因果链")
    card_running = builder.render_card()
    assert card_running["header"]["template"] == "blue"
    assert "[执行中]" in card_running["header"]["title"]["content"]
    body_text = card_running["elements"][0]["text"]["content"]
    assert "[RUNNING] 需求规格与因果链拆解 (正在解析因果链)" in body_text
    assert "[WAIT] 状态机条件与用例生成" in body_text

    # 3. Complete stage 1, advance stage 2
    clock_time += 20.0
    builder.advance("analysis", status="passed", note="提取断言完成")
    builder.advance("cases", status="running", note="逆向代码生成中")
    card_step2 = builder.render_card()
    body_text_2 = card_step2["elements"][0]["text"]["content"]
    assert "[OK] 需求规格与因果链拆解 (提取断言完成)" in body_text_2
    assert "[RUNNING] 状态机条件与用例生成 (逆向代码生成中)" in body_text_2

    # 4. Final completion
    clock_time += 30.0
    builder.advance("cases", status="passed")
    builder.advance("review", status="passed", note="得分 92.0")
    builder.advance("verify", status="passed")
    builder.complete(success=True, detail="所有验收门禁通过")
    builder.add_artifact("report.json")

    card_done = builder.render_card()
    assert card_done["header"]["template"] == "green"
    assert "[已完成]" in card_done["header"]["title"]["content"]
    assert any("report.json" in el.get("text", {}).get("content", "") for el in card_done["elements"])


def test_card_stream_patch_payload():
    builder = CardStreamBuilder(task_id="task-stream-002")
    builder.advance("analysis", status="running")
    patch = builder.render_patch_payload()

    assert patch["msg_type"] == "interactive"
    assert patch["card"]["config"]["update_multi"] is True
    assert "elements" in patch["card"]
    assert len(patch["card"]["elements"]) >= 2


def test_card_stream_sync_from_liveness():
    builder = CardStreamBuilder(task_id="task-stream-003")
    live_dict = {
        "ok": True,
        "task_id": "task-stream-003",
        "state": "running",
        "phase": "cases",
        "detail": "生成用例代码第 2 轮迭代",
        "artifacts": ["contract_requirements.yaml", "manifest.json"],
    }
    builder.sync_from_liveness(live_dict)

    card = builder.render_card()
    assert card["header"]["template"] == "blue"
    body = card["elements"][0]["text"]["content"]
    assert "[RUNNING] 状态机条件与用例生成" in body
    assert any("contract_requirements.yaml" in el.get("text", {}).get("content", "") for el in card["elements"])


def test_card_stream_dynamic_stages():
    builder = CardStreamBuilder(task_id="task-dyn-001")
    # Advance an unknown stage without failing
    builder.advance("custom_compile", status="running", note="正在编译二进制", name="编译产物生成")
    card = builder.render_card()
    body = card["elements"][0]["text"]["content"]
    assert "[RUNNING] 编译产物生成 (正在编译二进制)" in body

    # Sync from liveness with a completely novel phase
    builder.sync_from_liveness({
        "ok": True,
        "phase": "novel_verification_gate",
        "detail": "门禁动态触发",
    })
    card2 = builder.render_card()
    body2 = card2["elements"][0]["text"]["content"]
    assert "[RUNNING] novel_verification_gate" in body2


def test_card_frame_debouncer_and_backoff():
    from agentcore.card_stream import CardFrameDebouncer, CardStreamController

    current_time = 1000.0
    delays = []
    sent_payloads = []

    def fake_sleep(d):
        delays.append(d)

    attempts = 0

    def flaky_send(payload):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return {"ok": False, "retry_safe": True, "error": "network_timeout"}
        sent_payloads.append(payload)
        return {"ok": True, "edited": True}

    debouncer = CardFrameDebouncer(
        send_fn=flaky_send,
        min_interval=2.0,
        max_retries=3,
        backoff_initial=0.1,
        backoff_factor=2.0,
        clock=lambda: current_time,
        sleep_fn=fake_sleep,
    )

    # First update triggers send with retries
    res1 = debouncer.update({"frame": 1})
    assert res1["ok"] is True
    assert debouncer.emit_count == 1
    assert debouncer.retry_count == 2
    assert delays == [0.1, 0.2]
    assert len(sent_payloads) == 1

    # Second update within min_interval (2s) is debounced
    current_time += 0.5
    res2 = debouncer.update({"frame": 2})
    assert res2.get("debounced") is True
    assert debouncer.emit_count == 1

    # Terminal update (final=True) flushes immediately even within min_interval
    current_time += 0.2
    res3 = debouncer.update({"frame": 3, "terminal": True}, final=True)
    assert res3["ok"] is True
    assert debouncer.emit_count == 2
    assert sent_payloads[-1]["frame"] == 3


def test_card_stream_controller_integration():
    from agentcore.card_stream import CardStreamController

    now_clock = 1000.0
    emitted = []

    def transport(payload):
        emitted.append(payload)
        return {"ok": True}

    builder = CardStreamBuilder(task_id="task-ctrl-001", clock=lambda: now_clock)
    controller = CardStreamController(
        builder,
        send_fn=transport,
        min_interval=1.0,
        clock=lambda: now_clock,
    )

    # 1. Advance stage
    controller.advance("analysis", status="running", note="开始解析")
    assert len(emitted) == 1

    # 2. Debounced quick call
    now_clock += 0.2
    controller.advance("analysis", status="passed")
    assert len(emitted) == 1  # debounced

    # 3. Complete forces final flush
    now_clock += 0.1
    controller.complete(success=True, detail="任务全部完成")
    assert len(emitted) == 2
    assert "[已完成]" in emitted[-1]["card"]["header"]["title"]["content"]


def test_card_stream_status_aliases():
    builder = CardStreamBuilder(task_id="task-alias-001")
    # "done" and "success" map to [OK]
    builder.advance("analysis", status="done")
    builder.advance("cases", status="success")
    # "error" maps to [FAIL]
    builder.advance("review", status="error")
    # "cancelled" maps to [SKIP]
    builder.advance("verify", status="cancelled")

    card = builder.render_card()
    body = card["elements"][0]["text"]["content"]
    assert "[OK] 需求规格与因果链拆解" in body
    assert "[OK] 状态机条件与用例生成" in body
    assert "[FAIL] 红队 6 维度代码审查" in body
    assert "[SKIP] 静态 AST 与门禁验证" in body


def test_card_frame_debouncer_concurrency():
    import threading
    from agentcore.card_stream import CardFrameDebouncer

    sent = []

    def safe_send(payload):
        sent.append(payload)
        return {"ok": True}

    debouncer = CardFrameDebouncer(
        send_fn=safe_send,
        min_interval=0.01,
        max_retries=2,
    )

    errors = []

    def worker(worker_id):
        try:
            for i in range(20):
                debouncer.update({"worker": worker_id, "step": i})
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    debouncer.flush()
    assert not errors
    assert len(sent) >= 1
    assert debouncer.emit_count == len(sent)


def test_liveness_ledger_drain(tmp_path):
    from agentcore.liveness import LivenessLedger

    ledger = LivenessLedger(tmp_path / "drain_test.db")
    # 1. Empty ledger drains immediately
    res1 = ledger.drain(timeout=1.0)
    assert res1["ok"] is True
    assert res1["drained"] is True

    # 2. Register active task -> drain times out
    ledger.register("job-drain-1", kind="test", state="running")
    res2 = ledger.drain(timeout=0.1, poll_interval=0.05)
    assert res2["ok"] is False
    assert res2["drained"] is False
    assert "job-drain-1" in res2["active_tasks"]

    # 3. Finish task -> drain succeeds
    ledger.finish("job-drain-1", state="done")
    res3 = ledger.drain(timeout=1.0)
    assert res3["ok"] is True
    assert res3["drained"] is True


