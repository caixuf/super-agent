import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("combined", Path(__file__).resolve().parents[1] / "tools/recall_combined.py")
combined = importlib.util.module_from_spec(spec)
spec.loader.exec_module(combined)

def test_combines_sources_and_preserves_query(monkeypatch, capsys):
    calls = []
    def run(argv):
        calls.append(argv)
        values = ["规则", "相同"] if len(calls) == 1 else ["事实", "相同"]
        return [{"statement": v} for v in values]
    monkeypatch.setattr(combined, "run", run)
    monkeypatch.setattr(sys, "argv", ["recall_combined", "--query", "测试 查询", "--limit", "4", "--entity-db", "/e", "--data-dir", "/d", "--user", "u", "--agent", "a", "--profile", "p"])
    combined.main()
    assert [r["statement"] for r in json.loads(capsys.readouterr().out)["results"]] == ["规则", "事实", "相同"]
    assert all(any("测试 查询" in part for part in call) for call in calls)

def test_backend_failure_does_not_look_empty(monkeypatch):
    monkeypatch.setattr(combined.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="{\"ok\": false, \"results\": []}"))
    with pytest.raises(RuntimeError):
        combined.run(["fake"])


def test_real_backends_and_identity_isolation(tmp_path):
    import os
    import subprocess
    from agentcore.memory.config import MemoryConfig, configure, reset_config
    from agentcore.memory.entity_store import EntityStore
    from agentcore.memory.memory_store import write_fact, flush_background
    entity = tmp_path / "entities.db"
    EntityStore(db_path=entity)
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    configure(MemoryConfig(data_dir=data, default_user="u", default_agent="a", embed_fn=lambda texts: None))
    try:
        assert write_fact("3090 answers should be concise", subject="style", user_id="u", agent_id="a", tags={"profile": "p"}, schedule_background_index=False)["written"]
        assert flush_background(timeout=5) == 0
    finally:
        reset_config()
    command = [sys.executable, str(Path(combined.__file__)), "--query=3090", "--entity-db", str(entity), "--data-dir", str(data), "--user", "u", "--agent", "a", "--profile", "p"]
    env = dict(os.environ, MEMORY_OLLAMA_BASE_URL="http://127.0.0.1:1")
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout
    rows = json.loads(result.stdout)["results"]
    assert any("3090 answers should be concise" == row["statement"] for row in rows)
    assert any(row["statement"] != "3090 answers should be concise" for row in rows)
    command[-1] = "other"
    other = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    assert other.returncode == 0
    assert all(row["statement"] != "3090 answers should be concise" for row in json.loads(other.stdout)["results"])
