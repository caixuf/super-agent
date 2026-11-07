"""The re-deploy gate must verify the Hermes tree this instance actually runs.

``HERMES_AGENT_DIR`` wins when it is set. Otherwise the installed Hermes is
resolved from the ``hermes`` on PATH: the standard layout keeps it at
``~/.hermes/hermes-agent``, but an instance may install it elsewhere, and a
gate pointed at a tree nobody runs verifies nothing.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "redeploy_check_test", REPO / "tools" / "redeploy_check.py")
assert _spec and _spec.loader
redeploy_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(redeploy_check)


def test_explicit_agent_dir_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_AGENT_DIR", str(tmp_path))
    assert redeploy_check.hermes_source_tree() == tmp_path


def test_installed_hermes_on_path_is_resolved(monkeypatch, tmp_path):
    tree = tmp_path / "hermes-v0.22.5"
    (tree / "hermes_cli").mkdir(parents=True)
    launcher = tmp_path / "bin" / "hermes"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(tree / ".hermes" / "bin" / "hermes")
    monkeypatch.delenv("HERMES_AGENT_DIR", raising=False)
    monkeypatch.setattr(redeploy_check.shutil, "which", lambda _name: str(launcher))
    assert redeploy_check.hermes_source_tree() == tree


def test_standard_layout_is_the_last_resort(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_AGENT_DIR", raising=False)
    monkeypatch.setattr(redeploy_check.shutil, "which", lambda _name: None)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    assert redeploy_check.hermes_source_tree() == tmp_path / ".hermes" / "hermes-agent"


def test_an_already_patched_tree_is_skipped_not_failed(tmp_path, monkeypatch, capsys):
    """Re-applying a patch on top of an applied patch is not the same test.

    The installed tree carries the patches; the check proves they apply to a
    *clean* Hermes. Reporting that as a failure would look like a broken
    patch, so it is skipped with the reason spelled out.
    """
    agent = tmp_path / "agent"
    (agent / "gateway").mkdir(parents=True)
    (agent / "gateway" / "stream_consumer.py").write_text("x", encoding="utf-8")
    monkeypatch.setattr(redeploy_check, "hermes_source_tree", lambda: agent)
    monkeypatch.setattr(redeploy_check, "_modified_files",
                        lambda _agent, _files: ["gateway/stream_consumer.py"])
    result = redeploy_check.Result()
    redeploy_check.check_patches(result)
    assert result.failures == 0
    assert result.skipped == 1
    assert "pristine export" in capsys.readouterr().out
