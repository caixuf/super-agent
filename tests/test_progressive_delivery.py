"""Unit tests for the Progressive Delivery (渐进式交付) pattern.

Verifies:
1. Decoupling of Phase 1 text delivery from Phase 2 out-of-band diagram rendering.
2. Checkpoint creation, atomic persistence, and retrieval.
3. Card updates during Phase 1 and graceful degradation on Phase 2 timeout or crash.
4. Completion message envelope and queue receipt extraction.
5. Colleague evidence verification under progressive delivery and rendering failure.
6. Illustrated delivery guard passing Phase 1 progressive deliveries without false alarms.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time
from unittest.mock import MagicMock

import pytest

from agentcore.card_emit import CardEmitter
from agentcore.card_store import CardStore
from agentcore.colleague_evidence import verify_internal_result
from agentcore.completion_delivery import (
    CompletionMessage,
    build_message,
    is_terminal_status,
    notify_on_terminal,
)
from agentcore.pool import Pool
from agentcore.progressive_delivery import (
    ProgressiveDeliveryCoordinator,
    build_progressive_receipt,
    format_progressive_message,
    is_progressive_phase1,
    load_checkpoint,
    save_checkpoint,
)
from agentcore.task_card import CardState


def test_checkpoint_save_and_load(tmp_path: Path):
    target_dir = tmp_path / "attempt_1"
    summary = "Core text analysis: 7 pipeline stages defined and verified."
    report_md = "# Architecture Summary\n\nAll components analyzed.\n"

    saved = save_checkpoint(
        target_dir,
        job_id="job-101",
        attempt="att-01",
        summary=summary,
        report_md=report_md,
        phase="phase1_text",
        rendering_state="pending",
        artifacts=["report.md"],
    )

    assert saved["job"] == "job-101"
    assert saved["attempt"] == "att-01"
    assert saved["summary"] == summary
    assert (target_dir / "checkpoint.json").is_file()
    assert (target_dir / "report.md").read_text(encoding="utf-8") == report_md

    loaded = load_checkpoint(target_dir)
    assert loaded is not None
    assert loaded["job"] == "job-101"
    assert loaded["phase"] == "phase1_text"
    assert loaded["rendering_state"] == "pending"


def test_is_progressive_phase1_detection():
    assert is_progressive_phase1("分析完毕。\n\n##CHECKPOINT: phase1_ready\n正文如下")
    assert is_progressive_phase1("[渐进式交付] 核心文字结论已就绪")
    assert is_progressive_phase1("[PROGRESSIVE_DELIVERY] Text conclusions delivered.")
    assert is_progressive_phase1("核心方案已推导，出图作为异步附加物在后台渲染中。")
    assert is_progressive_phase1("分析完成，rendering in background...")

    # Plain text without progressive markers
    assert not is_progressive_phase1("分析完毕，共 8 条需求。")
    assert not is_progressive_phase1("")
    assert not is_progressive_phase1(None)


def test_format_progressive_message():
    formatted = format_progressive_message(
        "三仓职责划分推导完毕，流水线包含分诊、逆向、合成与跑测。",
        checkpoint_id="ckpt_998",
        rendering=True,
    )
    assert "[渐进式交付]" in formatted
    assert "##CHECKPOINT: ckpt_998" in formatted
    assert "出图作为异步附加物" in formatted
    assert "三仓职责划分推导完毕" in formatted


def test_progressive_coordinator_happy_path(tmp_path: Path):
    store = CardStore(tmp_path / "cards.db")
    store.bind("task-happy", "chat-1", "msg-1")
    transport = MagicMock()
    emitter = CardEmitter(store, "task-happy", transport, title="架构设计")

    coordinator = ProgressiveDeliveryCoordinator(card_emitter=emitter)

    # 1. Phase 1: Deliver text conclusion immediately (0-blocking)
    phase1_result = coordinator.deliver_phase1(
        "task-happy",
        "核心架构分析完成，7 个环节均已对齐。",
        artifacts=["report.md"],
    )
    assert phase1_result["ok"] is True
    assert phase1_result["phase"] == "phase1_text"

    # Card emitter should have sent an in-place update with phase="rendering"
    assert transport.emit.called
    handle = emitter.handle
    assert handle.state == CardState.RUNNING

    # 2. Phase 2: Out-of-band rendering succeeds
    def mock_render():
        return ["diagram.png", "architecture.html"]

    phase2_result = coordinator.run_out_of_band_rendering(
        mock_render,
        timeout_s=5.0,
        base_artifacts=["report.md"],
    )
    assert phase2_result["ok"] is True
    assert phase2_result["status"] == "completed"
    assert "diagram.png" in phase2_result["artifacts"]
    assert "architecture.html" in phase2_result["artifacts"]
    assert handle.state == CardState.DONE


def test_progressive_coordinator_rendering_timeout(tmp_path: Path):
    store = CardStore(tmp_path / "cards.db")
    store.bind("task-timeout", "chat-1", "msg-1")
    transport = MagicMock()
    emitter = CardEmitter(store, "task-timeout", transport, title="长图渲染")

    coordinator = ProgressiveDeliveryCoordinator(card_emitter=emitter)

    # Phase 1: Deliver text immediately
    coordinator.deliver_phase1(
        "task-timeout",
        "核心文字结论已落盘。",
        artifacts=["report.md"],
    )

    # Phase 2: Rendering takes longer than timeout_s
    def slow_render():
        time.sleep(2.0)
        return ["slow.png"]

    phase2_result = coordinator.run_out_of_band_rendering(
        slow_render,
        timeout_s=0.2,
        base_artifacts=["report.md"],
    )
    assert phase2_result["ok"] is True
    assert phase2_result["status"] == "degraded"
    assert phase2_result["reason"] == "rendering_timeout"
    assert phase2_result["artifacts"] == ["report.md"]
    # Card is finished gracefully without destroying Phase 1 delivery
    assert emitter.handle.state == CardState.DONE


def test_progressive_coordinator_rendering_exception(tmp_path: Path):
    store = CardStore(tmp_path / "cards.db")
    store.bind("task-fail", "chat-1", "msg-1")
    transport = MagicMock()
    emitter = CardEmitter(store, "task-fail", transport, title="异常测试")

    coordinator = ProgressiveDeliveryCoordinator(card_emitter=emitter)

    coordinator.deliver_phase1(
        "task-fail",
        "文字结论已交付。",
        artifacts=["report.md"],
    )

    def crashing_render():
        raise RuntimeError("headless_browser_crashed")

    phase2_result = coordinator.run_out_of_band_rendering(
        crashing_render,
        timeout_s=5.0,
        base_artifacts=["report.md"],
    )
    assert phase2_result["ok"] is True
    assert phase2_result["status"] == "degraded"
    assert "rendering_failed:RuntimeError" in phase2_result["reason"]
    assert emitter.handle.state == CardState.DONE


def test_completion_message_progressive_fields():
    msg = CompletionMessage(
        task_id="task-01",
        status="completed",
        summary="文字结论已交付",
        truncated=False,
        phase="phase1_text",
        checkpoint_delivered=True,
        rendering_state="completed",
    )
    rendered = msg.render()
    assert rendered["phase"] == "phase1_text"
    assert rendered["checkpoint_delivered"] is True
    assert rendered["rendering_state"] == "completed"


def test_build_message_progressive_extraction():
    receipt = {
        "job": "job-p1",
        "status": "completed",
        "result": {
            "ok": True,
            "result": {
                "phase": "phase1_text",
                "checkpoint_delivered": True,
                "rendering_state": "rendering",
                "summary": "阶段一文字结论：推导完成。",
                "artifacts": ["/tmp/report.md"],
            },
        },
    }
    msg = build_message(receipt)
    assert msg.phase == "phase1_text"
    assert msg.checkpoint_delivered is True
    assert msg.rendering_state == "rendering"
    assert "阶段一文字结论" in msg.summary
    assert msg.artifacts == ("/tmp/report.md",)


def test_is_terminal_or_checkpoint_status():
    assert is_terminal_status("completed")
    assert is_terminal_status("failed")
    assert is_terminal_status("cancelled")
    assert is_terminal_status("checkpoint_delivered")
    assert not is_terminal_status("running")
    assert not is_terminal_status("queued")


def test_colleague_evidence_accepts_progressive_delivery_and_degraded_rendering(tmp_path: Path):
    pool = Pool(tmp_path / "queue")
    job = pool.submit("job-prog-1", {"goal": "Analyze pipeline"}, replay_safe=True)
    row, fd = pool.claim()
    try:
        attempt = row["attempt"]
        attempt_dir = pool.root / "attempts" / attempt
        attempt_dir.mkdir(parents=True, exist_ok=True)

        manifest = {
            "job": job,
            "attempt": attempt,
            "verification": "progressive_delivery_v1",
            "worktree": "/tmp/worktree",
            "modified_files": ["docs/pipeline.md"],
        }
        report = {
            "job": job,
            "attempt": attempt,
            "verification": "progressive_delivery_v1",
            "summary": "Pipeline analysis complete.",
            "success": True,
            "text_delivered": True,
            "rendering_state": "failed",  # Browser rendering failed!
        }
        checkpoint = {
            "job": job,
            "attempt": attempt,
            "phase": "phase1_text",
            "summary": "Pipeline analysis complete.",
            "delivered": True,
            "rendering_state": "failed",
        }

        (attempt_dir / "manifest.json").write_text(json.dumps(manifest))
        (attempt_dir / "report.json").write_text(json.dumps(report))
        (attempt_dir / "checkpoint.json").write_text(json.dumps(checkpoint))

        receipt = {
            "ok": True,
            "result": {
                "job": job,
                "attempt": attempt,
                "verification": "progressive_delivery_v1",
                "summary": "Pipeline analysis complete.",
                "rendering_state": "failed",
            },
        }
        assert pool.finish(job, attempt, True, receipt)
    finally:
        import os
        os.close(fd)

    evidence = verify_internal_result(pool, job)
    assert evidence["attempt_id"] == attempt
    assert evidence["summary"] == "Pipeline analysis complete."
    assert evidence["verification"] == "progressive_delivery_v1"
    assert evidence["success"] is True
    assert evidence["rendering_state"] == "failed"


def test_illustrated_delivery_guard_passes_progressive_turn(tmp_path: Path):
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "plugins/illustrated_delivery/__init__.py"
    spec = importlib.util.spec_from_file_location("illustrated_test", path)
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)

    class Ctx:
        def __init__(self):
            self.hooks = {}
            self.config = {
                "enabled": True,
                "enforce_delivery": True,
                "engine_root": str(Path(__file__).resolve().parents[1]),
                "audit_path": str(tmp_path / "guard.jsonl"),
            }
        def get_config(self, k, d=None):
            return self.config.get(k, d)
        def register_hook(self, name, fn):
            self.hooks[name] = fn

    ctx = Ctx()
    plugin.register(ctx)

    # 1. Ask for an architecture introduction (artifact mode)
    ctx.hooks["pre_llm_call"](
        user_message="介绍一下系统的分层架构与协作流程",
        session_id="sess_1",
        turn_id="turn_1",
        platform="api",
    )

    # 2. Reply with Progressive Delivery Phase 1 text conclusion
    reply = (
        "[渐进式交付] 阶段一：核心架构结论已推导完毕。\n"
        "##CHECKPOINT: arch_phase1\n"
        "出图作为异步附加物：无头浏览器截图中，完成后将原地更新卡片。\n\n"
        "系统分为表现层、协调层与执行层，彼此正交解耦。"
    )

    transformed = ctx.hooks["transform_llm_output"](
        response_text=reply,
        session_id="sess_1",
        model="flash",
        platform="api",
    )
    # The guard MUST NOT block or append a warning!
    assert transformed is None


def test_dev_exec_runner_timeout_salvages_stdout(monkeypatch, tmp_path: Path):
    from install.worker import dev_exec_runner

    monkeypatch.setattr(dev_exec_runner, "_workdir", lambda: tmp_path)
    monkeypatch.setattr(dev_exec_runner, "_hermes_bin", lambda: "/bin/true")

    # Simulate subprocess timeout with stdout containing text analysis
    def fake_run(*args, **kwargs):
        err = subprocess.TimeoutExpired(cmd=["hermes"], timeout=10)
        err.stdout = "分析结论已完成：发现核心架构解耦点，详细报告见上文。"
        err.stderr = ""
        raise err

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = dev_exec_runner.run_hermes("分析核心架构")
    assert result["ok"] is True
    assert result["degraded"] is True
    assert result["degradation_reason"] == "runner_timeout_checkpoint_salvaged"
    assert result["phase"] == "phase1_text"
    assert "分析结论已完成" in result["summary"]
