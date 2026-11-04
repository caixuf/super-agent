"""Host-gated colleague pack tools stay default-off and absolute-path only."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from agentcore.colleague_pack import dump_pack_json, export_redacted

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "test_colleague_pack_plugin_mod",
    REPO / "plugins/colleague_catalog/__init__.py",
)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "colleague_pack"


def entry(name="Analyst"):
    return {
        "display_name": name,
        "duty": "Analyze supplied evidence",
        "can_do": ["Summarize supported findings"],
        "boundaries": ["Read only", "Do not infer missing evidence"],
    }


class Context:
    def __init__(self, settings):
        self.settings, self.tools, self.sections = settings, {}, {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs

    def register_system_prompt_section(self, **kwargs):
        self.sections[kwargs["id"]] = kwargs


def test_pack_tools_default_off():
    ctx = Context({"enabled": True, "roles": {"analysis": entry()}})
    plugin.register(ctx)
    assert set(ctx.tools) == {"colleague_catalog"}
    assert "colleague_pack_import" not in ctx.tools


def test_pack_tools_require_absolute_path(tmp_path):
    roles = {"analysis": entry()}
    ctx = Context({
        "enabled": True,
        "pack_import_export": True,
        "roles": roles,
    })
    plugin.register(ctx)
    assert set(ctx.tools) >= {"colleague_catalog", "colleague_pack_import", "colleague_pack_export"}

    imp = ctx.tools["colleague_pack_import"]["handler"]
    exp = ctx.tools["colleague_pack_export"]["handler"]
    assert json.loads(imp({"path": "relative.json"}, session_id="s")) == {
        "ok": False, "error": "absolute_pack_path_required",
    }
    assert json.loads(imp({"path": str((tmp_path / "x.json").resolve())}, session_id=None)) == {
        "ok": False, "error": "catalog_session_required",
    }

    pack_path = (tmp_path / "in.json").resolve()
    pack_path.write_text((FIXTURES / "happy.json").read_text(encoding="utf-8"), encoding="utf-8")
    result = json.loads(imp({"path": str(pack_path)}, session_id="s"))
    assert result["ok"] is True
    assert "scribe" in result["roles"]
    assert all(s["enabled"] is False for s in result["skills"])
    assert all(r["paused"] is True for r in result["routines"])
    # Host settings not mutated by the model path.
    assert ctx.settings["roles"] == roles

    out = (tmp_path / "out.json").resolve()
    exported = json.loads(exp({"path": str(out)}, session_id="s"))
    assert exported["ok"] is True and out.is_file()
    assert "not instructions or permissions" in exported["notice"]
