"""End-to-End LLM Semantic Intent Routing Evaluation (No Regex).

Verifies that the frontdesk LLM correctly understands complex, implicit,
and multi-intent natural language inputs to choose the correct Tool Call / Action:
1. Control Intent -> durable_cancel (never new submit)
2. Query Intent -> durable_status (never new submit)
3. Ambiguous Intent -> clarify (clarification gate, never blind dispatch)
4. SRE & Infrastructure Intent -> durable_submit(role='maintainer')
5. State Machine Transition Failure -> durable_submit(role='triage')
6. Test Case Authoring & Boundaries -> durable_submit(role='cases')
"""
from __future__ import annotations

import json
import urllib.request
import pytest

LOCAL_LLM_URL = "http://127.0.0.1:4000/v1/chat/completions"

SYSTEM_PROMPT = """你是智己汽车 MFF 自动化团队的仿真测试小助手（前台接待、超级参谋与敏捷协调中枢）。
你必须依据用户的真实语义意图进行准确决策：
1. 若用户意图是取消/撤回任务：调用 durable_cancel(task_id=...)，绝对不提交新任务；
2. 若用户意图是查询任务进度/状态：调用 durable_status(task_id=...)，绝对不提交新任务；
3. 若用户意图模糊宽泛（如'自检核心能力'）：直接输出文字反问确认，不调用任何工具；
4. 若用户意图是系统维护与基础设施排障（如容器挂了、网络不通、卡死）：调用 durable_submit(role='maintainer', goal=...)；
5. 若用户意图是业务工程任务：
   - 写用例/边界值 -> durable_submit(role='cases', goal=...)
   - 代码评审/300ms脱窗 -> durable_submit(role='reviewer', goal=...)
   - 容器跑测回归 -> durable_submit(role='test', goal=...)
   - 失败用例分诊/CM翻转/状态机不跳转 -> durable_submit(role='triage', goal=...)
   - 需求拆解与架构契约 -> durable_submit(role='architect', goal=...)
   - rosbag回灌对账 -> durable_submit(role='replay', goal=...)
严格以 JSON 输出决策：{"action": "call_tool" | "clarify", "tool_name": "...", "role": "...", "reasoning": "..."}"""


def query_llm_intent(user_prompt: str) -> dict:
    req = urllib.request.Request(
        LOCAL_LLM_URL,
        headers={"Content-Type": "application/json"},
        data=json.dumps({
            "model": "flash",
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"}
        }).encode("utf-8")
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
        return json.loads(body["choices"][0]["message"]["content"])


def is_llm_service_available() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:4000/v1/models", timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


@pytest.mark.skipif(not is_llm_service_available(), reason="Local LLM endpoint not available")
def test_llm_semantic_control_intent():
    decision = query_llm_intent("帮我把刚才那个任务 a8715492e85840babc33c462f24d847c 撤回一下，不用跑了")
    assert decision["tool_name"] == "durable_cancel"
    assert decision["action"] == "call_tool"


@pytest.mark.skipif(not is_llm_service_available(), reason="Local LLM endpoint not available")
def test_llm_semantic_query_intent():
    decision = query_llm_intent("查询下后台任务 a8715492e85840babc33c462f24d847c 的进度")
    assert decision["tool_name"] == "durable_status"
    assert decision["action"] == "call_tool"


@pytest.mark.skipif(not is_llm_service_available(), reason="Local LLM endpoint not available")
def test_llm_semantic_clarification_gate():
    decision = query_llm_intent("自检下当前核心能力，看看现在能做什么")
    assert decision["action"] == "clarify"


@pytest.mark.skipif(not is_llm_service_available(), reason="Local LLM endpoint not available")
def test_llm_semantic_infrastructure_intent():
    decision = query_llm_intent("Emily机器上的Docker容器退出码127，帮我排查下挂载问题")
    assert decision["tool_name"] == "durable_submit"
    assert decision["role"] == "maintainer"


@pytest.mark.skipif(not is_llm_service_available(), reason="Local LLM endpoint not available")
def test_llm_semantic_triage_vs_query_disambiguation():
    # Disambiguate "Standby 状态切不过去" (State Machine failure) from task progress query
    decision = query_llm_intent("这个用例在Standby状态一直切不过去，帮我定性一下为什么不跳转")
    assert decision["tool_name"] == "durable_submit"
    assert decision["role"] == "triage"


@pytest.mark.skipif(not is_llm_service_available(), reason="Local LLM endpoint not available")
def test_llm_semantic_cases_authoring_intent():
    decision = query_llm_intent("根据CM文档给ACC功能补充两组边界退出用例")
    assert decision["tool_name"] == "durable_submit"
    assert decision["role"] == "cases"
