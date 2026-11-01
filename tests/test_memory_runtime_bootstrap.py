import importlib.util
from pathlib import Path

import pytest

from agentcore.memory.config import get_config, reset_config

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("memory_bootstrap_test", REPO / "runtime/memory_bootstrap.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_bootstrap_sets_scoped_private_store_and_provider(tmp_path, monkeypatch):
    data = tmp_path / "memory"
    data.mkdir(mode=0o700)
    monkeypatch.setenv("SUPERAGENT_MEMORY_DATA_DIR", str(data))
    monkeypatch.setenv("SUPERAGENT_MEMORY_USER", "person-a")
    monkeypatch.setenv("SUPERAGENT_MEMORY_AGENT", "agent-a")
    monkeypatch.setenv("SUPERAGENT_MEMORY_EMBED_DIM", "64")
    seen = []
    monkeypatch.setattr(MODULE, "ccp_model", lambda messages, **kw: seen.append(kw) or "answer")
    try:
        MODULE.bootstrap()
        cfg = get_config()
        assert cfg.base_dir() == data and cfg.default_user == "person-a" and cfg.default_agent == "agent-a"
        assert cfg.embed_dim == 64
        assert cfg.llm_fn([{"role": "user", "content": "hello"}], timeout=10) == {"content": "answer"}
        assert seen == [{"alias": "flash", "timeout_seconds": 10.0}]
        assert list(data.iterdir()) == []
    finally:
        reset_config()


def test_bootstrap_rejects_nonprivate_storage(tmp_path, monkeypatch):
    data = tmp_path / "memory"
    data.mkdir(mode=0o755)
    monkeypatch.setenv("SUPERAGENT_MEMORY_DATA_DIR", str(data))
    monkeypatch.setenv("SUPERAGENT_MEMORY_USER", "person")
    monkeypatch.setenv("SUPERAGENT_MEMORY_AGENT", "agent")
    with pytest.raises(ValueError, match="private"):
        MODULE.bootstrap()


def test_isolated_event_to_scoped_recall(tmp_path, monkeypatch):
    import json
    import os
    import subprocess
    import sys
    from conftest import deterministic_embed
    from agentcore.memory import runtime_bridge, memory_store
    from agentcore.memory.event_worker import MemoryEventWorker
    from agentcore.memory.runtime_service import scoped_runner
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    monkeypatch.setenv("SUPERAGENT_MEMORY_DATA_DIR", str(data))
    monkeypatch.setenv("SUPERAGENT_MEMORY_USER", "user-a")
    monkeypatch.setenv("SUPERAGENT_MEMORY_AGENT", "agent-a")
    monkeypatch.setenv("SUPERAGENT_MEMORY_EMBED_DIM", "64")
    sentence = "以后回复我时保持简洁。"
    monkeypatch.setattr(MODULE, "ccp_model", lambda messages, **kwargs: json.dumps(
        {"facts": [{"statement": sentence, "user_quote": sentence, "subject": "style"}]}, ensure_ascii=False))
    try:
        MODULE.bootstrap()
        get_config().embed_fn = lambda texts: deterministic_embed(texts, 64)
        queue = runtime_bridge.TurnEventQueue(tmp_path / "queue.db")
        event = runtime_bridge.build_turn_event(profile="profile-a", user_id="user-a", agent_id="agent-a",
                                                session_id="session-a", turn_id="turn-a",
                                                user_message=sentence, assistant_reply="好的。", status="completed")
        assert queue.enqueue(event)["enqueued"]
        worker = MemoryEventWorker(queue=queue, event_runner=scoped_runner(
            profile="profile-a", user_id="user-a", agent_id="agent-a", queue=queue))
        outcome = worker.process_one()
        assert outcome["state"] == "done", outcome
        assert outcome["result"]["written"] == 1, outcome
        assert memory_store.flush_background(timeout=10) == 0
    finally:
        reset_config()
    env = dict(os.environ, MEMORY_OLLAMA_BASE_URL="http://127.0.0.1:1")
    result = subprocess.run([sys.executable, str(REPO / "tools/recall_memory.py"),
                             sentence, "--data-dir", str(data), "--user", "user-a",
                             "--agent", "agent-a", "--profile", "profile-a", "--embed-dim", "64"],
                            capture_output=True, text=True, timeout=30, env=env)
    assert result.returncode == 0, result.stderr
    assert sentence in [row["statement"] for row in json.loads(result.stdout)["results"]]
