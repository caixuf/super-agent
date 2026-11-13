"""Tests for structured backend-to-frontdesk completion report protocol."""
from __future__ import annotations

from agentcore.completion_delivery import build_message
from agentcore.structured_report import (
    extract_attachment_paths,
    normalize_structured_report,
    parse_structured_report,
    render_report_markdown,
)
from runtime.hermes_completion_transport import _render


def test_normalize_and_filter_internal_files():
    raw = {
        "conclusion": "回归验证通过，全部 12 条用例绿灯。",
        "highlights": ["通过率 12/12", "耗时 42.3s"],
        "files": [
            {"path": "/tmp/report.html", "name": "report.html", "description": "HTML 测试报告"},
            {"path": "/tmp/worker_result.json"},
        ],
        "images": [
            {"path": "/tmp/trend.png", "caption": "通过率趋势图"},
            {"path": "https://example.com/chart.png", "caption": "远程图"},
        ],
        "links": [
            {"title": "任务详情", "url": "https://example.com/task/123"},
            {"title": "非法链接", "url": "javascript:alert(1)"},
        ],
        "next_actions": ["可以合入主干"],
    }
    norm = normalize_structured_report(raw)
    assert norm is not None
    assert norm["schema_version"] == "1.1"
    assert [f["path"] for f in norm["files"]] == ["/tmp/report.html"]
    assert len(norm["images"]) == 2
    assert len(norm["links"]) == 1
    assert extract_attachment_paths(norm) == ["/tmp/report.html", "/tmp/trend.png"]


def test_parse_embedded_json_report_fence_and_xml():
    text = (
        "已检查完成。\n"
        "```json:report\n"
        '{"conclusion": "构建成功", "links": [{"title": "构建日志", "url": "https://ci.example.com/1"}]}\n'
        "```"
    )
    report, rem = parse_structured_report(text)
    assert report is not None
    assert report["conclusion"] == "构建成功"
    assert report["links"][0]["url"] == "https://ci.example.com/1"
    assert "已检查完成" in rem

    xml_text = '<report_json>{"conclusion": "盘点完成", "files": ["/tmp/out.md"]}</report_json>'
    report2, rem2 = parse_structured_report(xml_text)
    assert report2 is not None
    assert report2["files"][0]["path"] == "/tmp/out.md"
    assert rem2 == ""


def test_build_message_and_transport_preserve_structured_report():
    receipt = {
        "job": "job-struct-1",
        "status": "completed",
        "origin": {"profile": "frontdesk", "session": "s1", "channel": "feishu", "chat": "oc_1", "route": "feishu:oc_1"},
        "result": {
            "structured_report": {
                "conclusion": "3 项检查全部通过",
                "highlights": ["延迟 p50 278ms"],
                "files": [{"path": "/tmp/detail.md", "description": "详细报告"}],
                "images": [{"path": "/tmp/latency.png", "caption": "延迟分布"}],
                "links": [{"title": "看板", "url": "https://example.com/board"}],
                "next_actions": ["确认是否全量发布"],
            }
        },
    }
    msg = build_message(receipt)
    rendered = msg.render()
    assert rendered["structured_report"]["conclusion"] == "3 项检查全部通过"
    assert "/tmp/detail.md" in rendered["artifacts"]
    assert "/tmp/latency.png" in rendered["artifacts"]
    assert "https://example.com/board" in rendered["summary"]

    formatted = _render(rendered)
    assert "**结论**：3 项检查全部通过" in formatted
    assert "https://example.com/board" in formatted
    assert "延迟分布" in formatted


def test_async_incremental_report_merges_and_updates_bound_card(tmp_path):
    from agentcore.structured_report import merge_structured_report
    from agentcore.task_card_bridge import TaskCardBridge

    # Part 1: conclusion arrives first (final=False), images/files still pending
    p1 = {
        "seq": 1,
        "part_id": "text-1",
        "final": False,
        "parts_status": {"conclusion": "ready", "images": "pending", "files": "pending"},
        "conclusion": "阶段性结论：9900 平台全部绿灯",
        "highlights": ["通过率 100%"],
    }
    # Part 2: links & image arrive asynchronously later, and duplicate part_id is idempotent
    p2 = {
        "seq": 2,
        "part_id": "assets-2",
        "final": True,
        "parts_status": {"images": "ready", "files": "ready"},
        "links": [{"title": "测试报告", "url": "https://example.com/report/99"}],
        "images": [{"path": "/tmp/chart.png", "caption": "趋势图"}],
        "files": [{"path": "/tmp/report.html", "description": "完整报告"}],
    }
    m1 = merge_structured_report(None, p1)
    assert m1["final"] is False
    assert "异步生成中" in render_report_markdown(m1)
    m2 = merge_structured_report(m1, p2)
    m2_dup = merge_structured_report(m2, p2)
    assert m2 == m2_dup
    assert m2["final"] is True
    assert m2["conclusion"] == "阶段性结论：9900 平台全部绿灯"
    assert len(m2["links"]) == 1 and len(m2["images"]) == 1 and len(m2["files"]) == 1

    # Deep card binding: progressive update first, then final enrichment on the SAME message_id
    patches = []

    def sender(*, address, message_id, card_payload):
        patches.append((address, message_id, card_payload))
        return {"ok": True, "message_id": message_id}

    bridge = TaskCardBridge(tmp_path / "cards.db", profile="frontdesk", sender=sender)
    bridge.bind_task("t-async-1", {"route": "feishu:c1", "chat": "c1", "channel": "feishu"}, "跑回归", "test", "测试专家", message_id="om_card_1")

    r1 = bridge.apply_report_delta("t-async-1", p1, elapsed=3.2)
    assert r1["ok"] and r1["status"] == "progressive" and r1["final"] is False
    assert "阶段性结论" in bridge.get_card("t-async-1")["summary"]

    r2 = bridge.apply_report_delta("t-async-1", p2, elapsed=12.5)
    assert r2["ok"] and r2["status"] == "completed" and r2["final"] is True
    card = bridge.get_card("t-async-1")
    assert card["finalized"] == 1
    assert "/tmp/report.html" in card["artifacts_json"]

    # Even after finalized=1, an async late chart can still enrich the same card!
    r3 = bridge.apply_report_delta("t-async-1", {"seq": 3, "part_id": "late-img", "images": [{"path": "/tmp/extra.png", "caption": "补充图"}]}, elapsed=15.0)
    assert r3["ok"] and len(r3["structured_report"]["images"]) == 2
    assert len(patches) == 3


def test_refresh_running_cards_applies_progress_json_report_delta(tmp_path):
    from types import SimpleNamespace
    from plugins.durable_dispatch import DispatchService
    from agentcore.task_card_bridge import TaskCardBridge

    svc = DispatchService(str(tmp_path / "q"), "frontdesk", [str(tmp_path)], role="dev", task_cards_enabled=True, route_provider=lambda _s: {"route": "feishu:c1", "channel": "feishu", "chat": "c1"})
    sub = svc.submit("s1", {"goal": "跑一次检查", "request_key": "rk-prog-1"})
    job_id = sub["task_id"]
    svc.pool.claim("worker-1")
    attempt_id = svc.pool.status(job_id)["attempt"]
    with svc.pool.db() as db:
        db.execute("UPDATE jobs SET pid=12345 WHERE id=?", (job_id,))
        db.execute("UPDATE card_outbox SET state='sent', progress_state='sent', updated=1.0 WHERE job=?", (job_id,))
        db.commit()

    prog_dir = svc.pool.root / "attempts" / attempt_id
    prog_dir.mkdir(parents=True, exist_ok=True)
    (prog_dir / "progress.json").write_text(
        '{"phase": "生成图表中", "progress": 0.6, "structured_report": {"seq": 1, "part_id": "mid-1", "final": false, "conclusion": "前置检查已通过", "parts_status": {"images": "pending"}}}',
        encoding="utf-8",
    )

    patches = []
    bridge = TaskCardBridge(tmp_path / "cards.db", profile="frontdesk", sender=lambda *, address, message_id, card_payload: patches.append(card_payload) or {"ok": True, "message_id": message_id})
    bridge.bind_task(job_id, {"route": "feishu:c1", "chat": "c1", "channel": "feishu"}, "跑一次检查", "test", "测试专家", message_id="om_mid_1")

    outcomes = svc.refresh_running_cards(SimpleNamespace(card_bridge=bridge), min_age=0.0)
    assert len(outcomes) == 1
    assert len(patches) == 1
    card = bridge.get_card(job_id)
    assert card["status"] == "progressive"
    assert "前置检查已通过" in card["summary"]
