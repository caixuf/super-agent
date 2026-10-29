import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agentcore.colleague_catalog import MAX_CATALOG_BYTES, normalize_catalog, select_catalog


REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("test_colleague_catalog_plugin",
                                              REPO / "plugins/colleague_catalog/__init__.py")
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


def entry(name="Analyst"):
    return {"display_name": name, "duty": "Analyze supplied evidence",
            "can_do": ["Summarize supported findings"],
            "boundaries": ["Read only", "Do not infer missing evidence"]}


class Context:
    def __init__(self, settings):
        self.settings, self.tools, self.sections = settings, {}, {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs

    def register_system_prompt_section(self, **kwargs):
        self.sections[kwargs["id"]] = kwargs


def call(ctx, args, session_id="session-a"):
    return json.loads(ctx.tools["colleague_catalog"]["handler"](args, session_id=session_id))


def test_opt_in_registers_read_only_tool_and_safe_static_routing_hint():
    off = Context({"roles": {"analysis": entry()}})
    plugin.register(off)
    assert off.tools == {} and off.sections == {}
    ctx = Context({"enabled": True, "roles": {"analysis": entry()}})
    plugin.register(ctx)
    assert set(ctx.tools) == {"colleague_catalog"}
    assert set(ctx.sections) == {"colleague-catalog-routing"}
    prompt = ctx.sections["colleague-catalog-routing"]["content"]
    assert "untrusted" in prompt and "dispatch tool" in prompt
    assert "Analyze supplied evidence" not in prompt
    schema = ctx.tools["colleague_catalog"]["schema"]["parameters"]
    assert schema["additionalProperties"] is False and set(schema["properties"]) == {"role"}
    result = call(ctx, {})
    assert result["ok"] and result["colleagues"][0]["role"] == "analysis"
    assert "not instructions or permissions" in result["notice"]


def test_admin_data_is_descriptive_and_cannot_choose_backend_or_ownership():
    ctx = Context({"enabled": True, "roles": {"analysis": entry(), "review": entry("Reviewer")}})
    plugin.register(ctx)
    assert [x["role"] for x in call(ctx, {"role": "review"})["colleagues"]] == ["review"]
    assert call(ctx, {"role": "missing"})["colleagues"] == []
    for args in ({"role": "../escape"}, {"role": None}, {"role": "analysis", "owner": "forged"},
                 {"queue_root": "/tmp/forged"}, None, []):
        assert call(ctx, args) == {"ok": False, "error": "invalid_catalog_or_query"}
    assert call(ctx, {}, session_id=None) == {"ok": False, "error": "catalog_session_required"}


def test_profile_bound_config_is_read_per_call_and_disable_is_fail_closed():
    a = Context({"enabled": True, "roles": {"analysis": entry("A")}})
    b = Context({"enabled": True, "roles": {"review": entry("B")}})
    plugin.register(a)
    plugin.register(b)
    assert call(a, {})["colleagues"][0]["display_name"] == "A"
    assert call(b, {})["colleagues"][0]["display_name"] == "B"
    # Simulate a long-lived context reading another scoped profile on invocation.
    a.settings = b.settings
    assert call(a, {})["colleagues"][0]["display_name"] == "B"
    a.settings = {"enabled": False, "roles": {"analysis": entry("A")}}
    assert call(a, {}) == {"ok": False, "error": "catalog_disabled"}
    a.settings = {"enabled": True, "roles": {"analysis": {"display_name": "secret"}}}
    assert call(a, {}) == {"ok": False, "error": "invalid_catalog_or_query"}


@pytest.mark.parametrize("roles", [None, {}, [], {"../escape": entry()}, {True: entry()},
    {"analysis": {**entry(), "queue_root": "/tmp/private"}},
    {"analysis": {**entry(), "duty": "contains\ncontrol"}},
    {"analysis": {**entry(), "display_name": "spoof\u202ename"}},
    {"analysis": {**entry(), "boundaries": []}},
    {"analysis": {**entry(), "can_do": ["duplicate", "duplicate"]}}])
def test_invalid_admin_configuration_rejected_before_registration(roles):
    ctx = Context({"enabled": True, "roles": roles})
    with pytest.raises(ValueError):
        plugin.register(ctx)
    assert ctx.tools == {} and ctx.sections == {}


def test_catalog_has_bounded_output_and_never_mutates_configuration():
    roles = {"role_%d" % i: entry("Analyst %d" % i) for i in range(16)}
    original = copy.deepcopy(roles)
    result = normalize_catalog(roles)
    assert roles == original and len(result["colleagues"]) == 16
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= MAX_CATALOG_BYTES
    result["colleagues"][0]["can_do"].append("mutated")
    assert roles == original
    with pytest.raises(ValueError):
        normalize_catalog({**roles, "extra": entry()})
    oversized = {"role_%d" % i: {"display_name": "甲" * 80, "duty": "乙" * 300,
                 "can_do": ["丙" * 179 + str(n) for n in range(6)],
                 "boundaries": ["丁" * 179 + str(n) for n in range(6)]} for i in range(16)}
    with pytest.raises(ValueError, match="catalog_output_budget"):
        normalize_catalog(oversized)


def test_catalog_selection_is_deterministic():
    roles = {"review": entry("Reviewer"), "analysis": entry()}
    assert [item["role"] for item in select_catalog(roles, {})["colleagues"]] == ["analysis", "review"]


def test_catalog_description_cannot_grant_access_to_dispatcher(tmp_path):
    spec = importlib.util.spec_from_file_location("catalog_dispatch_test",
                                                  REPO / "plugins/durable_dispatch/__init__.py")
    dispatch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dispatch)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside policy")
    roles = {"analysis": {**entry(), "can_do": ["Read any file"]}}
    assert select_catalog(roles, {})["colleagues"][0]["can_do"] == ["Read any file"]
    service = dispatch.DispatchService(str(tmp_path / "queue"), "profile-a", [str(allowed)])
    with pytest.raises(PermissionError):
        service.submit("session-a", {"goal": "Read evidence", "request_key": "one",
                                      "sources": [{"path": str(outside)}]})
    with service.pool.db() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_actual_hermes_tool_registry_dispatch(tmp_path):
    host = Path(os.environ.get("HERMES_SOURCE", Path.home() / ".hermes/hermes-agent"))
    if not (host / "tools/registry.py").exists():
        pytest.skip("Hermes source unavailable for real registry test")
    code = '''
import importlib.util,json,sys
sys.path.insert(0,sys.argv[1]);sys.path.insert(0,sys.argv[2])
from tools.registry import ToolRegistry
spec=importlib.util.spec_from_file_location("catalog_host_test",sys.argv[3])
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
registry=ToolRegistry()
class Context:
    def get_config(self,key,default=None):
        return {"enabled":True,"roles":{"analysis":{"display_name":"Analyst",
          "duty":"Review evidence","can_do":["Review"],"boundaries":["Read only"]}}}.get(key,default)
    def register_tool(self,**kw):registry.register(**kw)
    def register_system_prompt_section(self,**kw):pass
p.register(Context())
assert json.loads(registry.dispatch("colleague_catalog",{}))["ok"] is False
assert json.loads(registry.dispatch("colleague_catalog",{},session_id="s"))["colleagues"][0]["role"]=="analysis"
print("registry integration passed")
'''
    env = dict(os.environ, HERMES_HOME=str(tmp_path / "isolated-home"))
    result = subprocess.run([sys.executable, "-c", code, str(host), str(REPO),
                             str(REPO / "plugins/colleague_catalog/__init__.py")],
                            env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-3000:]
    assert "registry integration passed" in result.stdout
