from agentcore.colleague_gateway_service import _present_status


def test_terminal_turn_is_not_reported_complete_before_background_evidence():
    status = _present_status({
        "state": "terminal", "outcome": "completed",
        "reservation": "awaiting_quiescence_evidence",
        "result": {"completed": True, "failed": False, "interrupted": False, "final_response": "答复"},
    })
    assert "已回复" in status["status_message"]
    assert "待确认" in status["status_message"]
    assert "已完成任务" not in status["status_message"]


def test_only_verified_reply_with_closed_reservation_is_complete():
    base = {"state": "terminal", "outcome": "completed", "reservation": "closed"}
    assert "已完成任务" not in _present_status({**base, "result": None})["status_message"]
    assert "已完成任务" not in _present_status({**base, "outcome": None,
                                                  "result": {"completed": True}})["status_message"]
    assert "已完成任务" not in _present_status({**base, "result": {"completed": True,
        "failed": True, "interrupted": False}})["status_message"]
    result = _present_status({**base, "result": {"completed": True, "failed": False,
                                                  "interrupted": False, "response_truncated": True}})
    assert "已完成任务" in result["status_message"]
    assert "截断" in result["status_message"]


def test_failure_and_uncertainty_are_distinct_from_success():
    failed = _present_status({"state": "terminal", "outcome": "failed", "reservation": "open"})
    unknown = _present_status({"state": "unknown"})
    assert "未完成" in failed["status_message"] and "待确认" in failed["status_message"]
    assert "待确认" in unknown["status_message"] and "不要重复派工" in unknown["status_message"]
