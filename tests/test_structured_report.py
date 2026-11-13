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
    assert norm["schema_version"] == "1.0"
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
