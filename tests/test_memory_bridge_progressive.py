import importlib.util
import json
from pathlib import Path

from agentcore.memory.config import MemoryConfig, configure, reset_config
from agentcore.memory.memory_store import write_fact

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("memory_bridge_progressive_test", REPO / "plugins/memory_bridge/__init__.py")
PLUGIN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLUGIN)


class Context:
    profile_name = "p1"
    def __init__(self, settings):
        self.settings, self.tools, self.hooks = settings, {}, {}
    def get_config(self, key, default=None):
        return self.settings.get(key, default)
    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs
    def register_hook(self, name, callback):
        self.hooks[name] = callback


def test_progressive_tools_register_and_use_scoped_store(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    configure(MemoryConfig(data_dir=data, default_user="u1", default_agent="a1", embed_fn=lambda texts: None))
    try:
        own = write_fact("I prefer concise answers", subject="style", user_id="u1", agent_id="a1",
                         tags={"profile": "p1"}, schedule_background_index=False)["fact_id"]
        foreign = write_fact("other person's private fact", subject="private", user_id="other", agent_id="a1",
                             tags={"profile": "p1"}, schedule_background_index=False)["fact_id"]
    finally:
        reset_config()
    ctx = Context({"enabled": True, "engine_root": str(REPO), "spool_dir": str(tmp_path / "spool"),
                   "data_dir": str(data), "user_id": "u1", "agent_id": "a1"})
    PLUGIN.register(ctx)
    assert set(ctx.tools) == {"memory_search_l1", "memory_timeline_l2", "memory_get_observations_l3"}
    assert all("parameters" in row["schema"] for row in ctx.tools.values())
    handler = ctx.tools["memory_get_observations_l3"]["handler"]
    assert json.loads(handler({"ids": [own]}, session_id=None))["error"] == "memory_session_required"
    result = json.loads(handler({"ids": [own, foreign]}, session_id="session"))
    assert result["ok"] is True
    assert [row["fact_id"] for row in result["results"]] == [own]
    injected = json.loads(handler({"ids": [own], "user_id": "other"}, session_id="session"))
    assert injected["ok"] is False


def test_existing_bridge_without_bound_store_does_not_register_tools(tmp_path):
    ctx = Context({"enabled": True, "engine_root": str(REPO), "spool_dir": str(tmp_path / "spool"),
                   "user_id": "u1", "agent_id": "a1"})
    PLUGIN.register(ctx)
    assert not ctx.tools
    assert "post_llm_call" in ctx.hooks
