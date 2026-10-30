"""Tests for agentcore.codegraph AST and topology module."""
from __future__ import annotations

from pathlib import Path
import agentcore.codegraph as cg


def test_codegraph_build_and_summarize(tmp_path: Path) -> None:
    # Create sample Python project structure
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from .mod import foo\n", encoding="utf-8")
    (pkg / "mod.py").write_text("def foo():\n    return 42\n", encoding="utf-8")
    
    graph = cg.build_codegraph(tmp_path)
    assert "files" in graph
    assert "symbols" in graph
    assert len(graph["files"]) == 2
    
    summary = cg.summarize_codegraph(graph)
    assert summary["stats"]["file_count"] == 2
    assert summary["stats"]["symbol_count"] >= 1
    
    text = cg.render_summary_text(graph)
    assert "Stats" in text
    assert "Files: 2 Python" in text


def test_codegraph_test_selection(tmp_path: Path) -> None:
    # Test changes selection
    (tmp_path / "app.py").write_text("def main(): pass\n", encoding="utf-8")
    (tmp_path / "test_app.py").write_text("import app\ndef test_main(): app.main()\n", encoding="utf-8")
    
    graph = cg.build_codegraph(tmp_path)
    selection = cg.select_tests_for_changes(graph, changed=["app.py"])
    assert "selected_tests" in selection
    assert "selected_test_paths" in selection
