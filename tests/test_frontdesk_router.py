"""Unit tests for the frontdesk pre-routing workflow in local_model_grounding."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from plugins.local_model_grounding import Grounding
from plugins.local_model_grounding.client import LocalModelClient
from plugins.local_model_grounding.router import (
    FrontdeskRouter,
    build_routing_text,
    fastpath_route,
)


def _write_roles_spec(tmp_path: Path) -> Path:
    spec = {
        "frontdesk": {
            "query_intents": {
                "health": {"description": "Check service health", "action": "check_health.py"},
                "task_status": {"description": "Check background task status", "action": "durable_status"},
            }
        },
        "roles": {
            "builder": {"display_name": "Build Specialist(Bob)", "duty": "Compile and package artifacts"},
            "qa": {"display_name": "QA Specialist", "duty": "Write and review test cases"},
        },
    }
    path = tmp_path / "roles_spec.json"
    path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    return path


def test_fastpath_and_context_building(tmp_path: Path):
    spec_path = _write_roles_spec(tmp_path)
    client = LocalModelClient("")
    router = FrontdeskRouter(client, roles_spec_path=str(spec_path))
    catalog = router.get_catalog()
    assert catalog is not None
    assert "query:health" in catalog.key_to_option
    assert "builder" in catalog.key_to_option

    assert fastpath_route("取消任务", catalog)["action"] == "durable_cancel"
    assert fastpath_route("同事的忙碌情况", catalog)["action"] == "durable_roster"
    assert fastpath_route("看下修改那单的进度", catalog)["target"] == "query:task_status"
    assert fastpath_route("@Bob 帮我看看编译", catalog)["target"] == "builder"

    text = build_routing_text(
        "可以去服务器实地查看下",
        [{"role": "user", "content": "查一下最近测试情况"}, {"role": "assistant", "content": "好的"}],
    )
    assert "[最近上下文]" in text
    assert "查一下最近测试情况" in text
    assert "可以去服务器实地查看下" in text


def test_frontdesk_router_decider_threshold_and_fail_open(tmp_path: Path):
    spec_path = _write_roles_spec(tmp_path)
    audits = []
    client = LocalModelClient("http://127.0.0.1:18920")
    router = FrontdeskRouter(client, roles_spec_path=str(spec_path), min_confidence=0.7, audit_fn=audits.append)

    with patch.object(client, "decide", return_value={"choice": "qa: QA Specialist(Write and review test cases)", "confidence": 0.88, "elapsed_ms": 120.0}):
        res = router.route("帮我补齐边界测试用例", session_id="s1")
    assert res is not None
    assert res["target"] == "qa"
    assert 'durable_submit(role="qa"' in res["hint"]
    assert audits[-1]["injected"] is True

    with patch.object(client, "decide", return_value={"choice": "qa: QA Specialist", "confidence": 0.55, "elapsed_ms": 110.0}):
        low = router.route("有点模糊的问题", session_id="s2")
    assert low is None
    assert audits[-1]["injected"] is False


def test_openai_decider_backend_parses_json_schema():
    client = LocalModelClient("http://127.0.0.1:18081", decider_backend="openai", model_name="decider-4b")
    fake_resp = {
        "model": "decider-4b",
        "choices": [{"message": {"content": '{"target": "builder", "confidence": 0.91}'}}],
    }
    with patch.object(client, "_post", return_value=fake_resp) as post_mock:
        verdict = client.decide("帮我编译一下", "路由给谁？", ["self: 前台", "builder: 编译专家"])
    assert verdict is not None
    assert verdict["choice"] == "builder: 编译专家"
    assert verdict["confidence"] == 0.91
    assert post_mock.call_args[0][0] == "/v1/chat/completions"


def test_grounding_service_pre_llm_call_injects_route_hint(tmp_path: Path):
    spec_path = _write_roles_spec(tmp_path)

    class _Ctx:
        profile_name = "frontdesk"

        def __init__(self):
            self.cfg = {
                "gateway_url": "http://127.0.0.1:18920",
                "frontdesk_routing_enabled": True,
                "roles_spec_path": str(spec_path),
                "audit_path": str(tmp_path / "audit.jsonl"),
            }

        def get_config(self, key, default=None):
            return self.cfg.get(key, default)

    svc = Grounding(_Ctx())
    with patch.object(svc.client, "decide", return_value={"choice": "query:health: 轻量只读查询(Check service health)", "confidence": 0.92, "elapsed_ms": 80.0}):
        out = svc.pre_llm_call(session_id="chat-1", user_message="平台现在健康吗")
    assert out is not None
    assert "target=query:health" in out["context"]
    assert "check_health.py" in out["context"]
