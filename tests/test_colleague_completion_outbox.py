import pytest
from agentcore.pool import Pool


def test_colleague_completion_is_idempotent_and_keeps_original_origin(tmp_path):
    pool = Pool(tmp_path / "queue")
    origin = {"profile": "caixuf", "session": "session-1", "route": "feishu:chat-1", "channel": "feishu", "chat": "chat-1"}
    member = {"role_id": "cases", "display_name": "用例开发", "role_label": "用例"}
    kwargs = dict(source_id="a" * 32, origin=origin, created=100.0, status="completed", summary="完成", member=member)
    job = pool.record_colleague_completion(**kwargs)
    assert pool.record_colleague_completion(**kwargs) == job
    with pool.db() as db:
        assert db.execute("select count(*) from jobs").fetchone()[0] == 1
        assert db.execute("select count(*) from outbox").fetchone()[0] == 1
    assert pool.claim_receipt(not_before=101.0) is None
    with pytest.raises(ValueError, match="colleague_completion_conflict"):
        pool.record_colleague_completion(**{**kwargs, "summary": "不同结果"})
    with pytest.raises(ValueError, match="bound_delivery_route_required"):
        pool.record_colleague_completion(**{**kwargs, "origin": {"profile": "caixuf", "session": "session-1"}})


def test_colleague_outbox_flows_through_existing_direct_delivery(tmp_path):
    from agentcore.completion_delivery import deliver_once
    from agentcore.direct_completion import DirectCompletionTransport
    pool = Pool(tmp_path / "queue")
    origin = {"profile": "caixuf", "session": "session-1", "route": "feishu:chat-1", "channel": "feishu", "chat": "chat-1"}
    member = {"role_id": "cases", "display_name": "用例开发", "role_label": "用例"}
    job = pool.record_colleague_completion(source_id="b" * 32, origin=origin, created=100.0,
                                           status="completed", summary="完成", member=member)
    sent = []
    def sender(address, item, *, idempotency_key):
        sent.append((address, item, idempotency_key))
        return {"ok": True, "message_id": "message-1"}
    transport = DirectCompletionTransport(tmp_path / "delivery", profile="caixuf", sender=sender,
        render=lambda message: message["summary"], attempt_root=tmp_path / "attempts",
        attempt_provider=lambda task_id: pool.status(task_id).get("attempt"))
    assert deliver_once(pool, transport)["status"] == "delivered"
    assert deliver_once(pool, transport)["status"] == "empty"
    assert len(sent) == 1 and sent[0][0] == origin and sent[0][1]["text"] == "完成"
    assert pool.status(job)["status"] == "completed"
