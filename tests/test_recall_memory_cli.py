import json
import os
from pathlib import Path
import subprocess
import sys

from agentcore.memory.config import MemoryConfig, configure, reset_config
from agentcore.memory.memory_store import write_fact, flush_background

REPO = Path(__file__).resolve().parents[1]


def run(root, user, agent, profile):
    env = dict(os.environ, MEMORY_OLLAMA_BASE_URL="http://127.0.0.1:1")
    return subprocess.run([sys.executable, str(REPO / "tools/recall_memory.py"),
                           "prefer concise answers", "--data-dir", str(root),
                           "--user", user, "--agent", agent, "--profile", profile],
                          env=env, capture_output=True, text=True, timeout=30)


def test_recall_reads_same_store_and_enforces_principal(tmp_path):
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    configure(MemoryConfig(data_dir=root, default_user="u1", default_agent="a1",
                           embed_fn=lambda texts: None))
    try:
        result = write_fact("I prefer concise answers", subject="answer style",
                            user_id="u1", agent_id="a1", tags={"profile": "p1"},
                            schedule_background_index=False)
        assert result["written"] is True
        assert flush_background(timeout=5) == 0
    finally:
        reset_config()
    same = run(root, "u1", "a1", "p1")
    assert same.returncode == 0, same.stderr
    found = json.loads(same.stdout)
    assert any(r["statement"] == "I prefer concise answers" for r in found["results"])
    for user, agent, profile in [("other", "a1", "p1"), ("u1", "other", "p1"), ("u1", "a1", "other")]:
        response = run(root, user, agent, profile)
        assert response.returncode == 0, response.stderr
        assert json.loads(response.stdout)["results"] == []


def test_missing_store_is_an_error_not_empty_memory(tmp_path):
    root = tmp_path / "missing"
    root.mkdir(mode=0o700)
    response = run(root, "u", "a", "p")
    assert response.returncode != 0
    assert json.loads(response.stdout)["ok"] is False
    assert list(root.iterdir()) == []
