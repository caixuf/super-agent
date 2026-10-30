import json

from agentcore.memory import memory_store, memory_trigger


def _model(quote, statement="用户偏好更简洁"):
    def call(messages, **options):
        assert options == {"timeout_seconds": 45}
        assert messages[0]["role"] == "system"
        return json.dumps({"facts": [{"statement": statement, "subject": "沟通偏好",
                                     "user_quote": quote}]}, ensure_ascii=False)
    return call


def test_candidate_uses_user_evidence_and_never_writes_memory(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("candidate extraction must not write memory")
    monkeypatch.setattr(memory_store, "write_fact", forbidden)
    text = "以后回答请简洁一点。"
    result = memory_trigger.propose_facts(text, model=_model(text))
    assert result["ok"] is True and result["reason"] == "review_required"
    assert result["candidates"][0]["statement"] == text


def test_unverified_quote_and_failed_model_are_not_empty_success():
    result = memory_trigger.propose_facts("以后回答请简洁一点。", model=_model("不存在的引用"))
    assert result == {"ok": False, "reason": "model_unverified_user_evidence", "candidates": []}
    def failed(*_args, **_kwargs):
        raise RuntimeError("secret error")
    result = memory_trigger.propose_facts("以后回答请简洁一点。", model=failed)
    assert result == {"ok": False, "reason": "model_call_failed", "candidates": []}


def test_question_short_lived_and_incomplete_turn_do_not_become_candidates():
    def forbidden(*_args, **_kwargs):
        raise AssertionError("model should not be invoked")
    assert memory_trigger.propose_facts("以后要改成这个吗？", model=forbidden)["candidates"] == []
    assert memory_trigger.propose_facts("以后回答请简洁一点。", model=forbidden,
                                        turn_status="failed")["reason"] == "turn_not_completed"
    text = "这次不要改文件，我决定先看结果。"
    result = memory_trigger.propose_facts(text, model=_model("这次不要改文件"))
    assert result["ok"] is True and result["candidates"] == []
