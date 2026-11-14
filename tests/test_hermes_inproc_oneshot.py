"""Unit tests for the unattended backend worker narration continuation guard."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "install/worker/hermes_inproc_oneshot.py"
spec = importlib.util.spec_from_file_location("hermes_inproc_oneshot", SCRIPT)
oneshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oneshot)


@pytest.mark.parametrize(
    "text",
    [
        "hnrss 与 HN 的 Algolia API 都能通；以 HN 官网域名访问会超时，但换用同源的两个接口就可用。现在解析国内源和 HN。",
        "关键风险点已经抓到，最后做一次完整可行的验证",
        "正在抓取并汇总各新闻源标题。",
        "前置脚本已生成，接下来运行验证脚本。",
        "Found the RSS feeds. Let me now parse the titles and summarize.",
    ],
)
def test_incomplete_backend_narration_detected(text):
    assert oneshot._is_incomplete_backend_narration(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        None,
        "全部检查通过，共 12 条新闻摘要。",
        "# 今日 AI 新闻汇总\n\n现在解析如下内容。",
        "- 第一条结论\n- 第二条结论\n- 第三条结论",
        "```python\nprint('现在解析国内源')\n```",
        "如果你批准，我现在执行检查。",
        "先检查日志已经完成。",
    ],
)
def test_complete_or_gated_backend_responses_not_flagged(text):
    assert not oneshot._is_incomplete_backend_narration(text)


def test_continue_incomplete_backend_turn_recovers_full_deliverable():
    agent = MagicMock()
    prior_messages = [{"role": "user", "content": "汇总今日新闻"}, {"role": "assistant", "content": "现在解析国内源和 HN。"}]
    agent.run_conversation.return_value = {
        "completed": True,
        "final_response": "# 今日 AI 新闻\n\n1. OpenAI 发布更新\n2. DeepMind 发布论文\n3. Anthropic 更新 SDK",
        "messages": prior_messages + [{"role": "assistant", "content": "final"}],
        "api_calls": 2,
        "input_tokens": 300,
        "output_tokens": 200,
        "total_tokens": 500,
        "estimated_cost_usd": 0.02,
    }
    initial = {
        "completed": True,
        "final_response": "hnrss 与 HN 的 Algolia API 都能通。现在解析国内源和 HN。",
        "messages": prior_messages,
        "api_calls": 6,
        "input_tokens": 1000,
        "output_tokens": 70,
        "total_tokens": 1070,
        "estimated_cost_usd": 0.05,
    }
    merged = oneshot._continue_incomplete_backend_turn(agent, initial)
    agent.run_conversation.assert_called_once_with(
        oneshot._BACKEND_CONTINUATION_NUDGE,
        conversation_history=prior_messages,
    )
    assert merged["completed"] is True
    assert merged["final_response"].startswith("# 今日 AI 新闻")
    assert merged["api_calls"] == 8
    assert merged["total_tokens"] == 1570
    assert merged["estimated_cost_usd"] == pytest.approx(0.07)


def test_continue_incomplete_backend_turn_marks_partial_when_still_narration():
    agent = MagicMock()
    agent.run_conversation.return_value = {
        "completed": True,
        "final_response": "正在继续解析国内源。",
        "messages": [],
        "api_calls": 1,
    }
    initial = {
        "completed": True,
        "final_response": "现在解析国内源和 HN。",
        "messages": [],
        "api_calls": 2,
    }
    merged = oneshot._continue_incomplete_backend_turn(agent, initial, max_continuations=2)
    assert agent.run_conversation.call_count == 2
    assert merged["completed"] is False
    assert merged["partial"] is True
    assert merged["turn_exit_reason"] == "incomplete_narration"
    assert merged["api_calls"] == 4


def test_continue_incomplete_backend_turn_recovers_from_guardrail_halt():
    agent = MagicMock()
    agent.run_conversation.return_value = {
        "completed": True,
        "final_response": "**结论**：emily 9900 当前健康。",
        "turn_exit_reason": "stop",
        "messages": [],
        "api_calls": 2,
    }
    initial = {
        "completed": True,
        "final_response": "I stopped retrying because I kept running search_files 4 times without making progress. Tell me how you'd like to proceed, or send `continue` and I'll try a different approach.",
        "turn_exit_reason": "guardrail_halt",
        "messages": [],
        "api_calls": 29,
    }
    merged = oneshot._continue_incomplete_backend_turn(agent, initial)
    agent.run_conversation.assert_called_once_with(
        oneshot._GUARDRAIL_HALT_NUDGE,
        conversation_history=[],
    )
    assert merged["completed"] is True
    assert merged["final_response"].startswith("**结论**")
    assert merged["api_calls"] == 31


def test_continue_incomplete_backend_turn_replaces_english_guardrail_fallback():
    agent = MagicMock()
    agent.run_conversation.return_value = {
        "completed": True,
        "final_response": "I stopped retrying because I kept running search_files 4 times without making progress. Tell me how you'd like to proceed, or send `continue` and I'll try a different approach.",
        "turn_exit_reason": "guardrail_halt",
        "messages": [],
        "api_calls": 1,
    }
    initial = {
        "completed": True,
        "final_response": "I stopped retrying because I kept running search_files 4 times without making progress. Tell me how you'd like to proceed, or send `continue` and I'll try a different approach.",
        "turn_exit_reason": "guardrail_halt",
        "messages": [],
        "api_calls": 4,
    }
    merged = oneshot._continue_incomplete_backend_turn(agent, initial)
    assert agent.run_conversation.call_count == 1
    assert merged["completed"] is False
    assert merged["partial"] is True
    assert merged["turn_exit_reason"] == "guardrail_halt"
    assert merged["final_response"] == oneshot._GUARDRAIL_HALT_FALLBACK
    assert merged["api_calls"] == 5
