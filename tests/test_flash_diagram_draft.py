import json

import pytest

from agentcore.flash_diagram_draft import DraftError, draft_diagram


def _model(graph):
    def call(messages, **options):
        assert options == {"timeout_seconds": 45}
        assert messages[0]["role"] == "system"
        return json.dumps(graph, ensure_ascii=False)
    return call


def test_draft_is_source_linked_and_never_claimed_as_complete():
    graph = {"nodes": [{"id": "main", "label": "主机器人", "fact": 1},
                        {"id": "fast", "label": "快速模型", "fact": 1}],
             "edges": [{"from": "main", "to": "fast", "fact": 1}]}
    result = draft_diagram(["主机器人把简单任务交给快速模型。"], model=_model(graph))
    assert result == {**graph, "review_required": True}


def test_graph_rejects_invented_nodes_and_unsupported_edge_evidence():
    facts = ["主机器人接收任务。", "快速模型处理简单任务。"]
    invented = {"nodes": [{"id": "x", "label": "调度器", "fact": 1}], "edges": []}
    with pytest.raises(DraftError, match="unverified_node"):
        draft_diagram(facts, model=_model(invented))
    edge = {"nodes": [{"id": "main", "label": "主机器人", "fact": 1},
                       {"id": "fast", "label": "快速模型", "fact": 2}],
            "edges": [{"from": "main", "to": "fast", "fact": 1}]}
    with pytest.raises(DraftError, match="unverified_edge"):
        draft_diagram(facts, model=_model(edge))


def test_model_failure_does_not_expose_provider_error():
    def failed(*_args, **_kwargs):
        raise RuntimeError("sensitive provider error")
    with pytest.raises(DraftError, match="^model_call_failed$"):
        draft_diagram(["一条事实"], model=failed)
