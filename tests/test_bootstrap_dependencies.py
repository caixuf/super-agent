import importlib.util
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("bootstrap_dependencies_test", REPO / "install/bootstrap_dependencies.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_missing_browser_is_installed_into_managed_cache(tmp_path, monkeypatch):
    root = tmp_path / "engine"
    python = root / ".superagent-venv/bin/python"
    python.parent.mkdir(parents=True)
    python.touch()
    calls = []
    monkeypatch.setattr(MODULE, "ready", lambda p: True)
    monkeypatch.setattr(MODULE.shutil, "which", lambda n: None)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "empty-cache"))
    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "playwright" in argv and "install" in argv and "chromium" in argv:
            chrome = root / ".browser-cache/chromium-123/chrome-linux64/chrome"
            chrome.parent.mkdir(parents=True)
            chrome.write_text("#!/bin/sh\nexit 0\n")
            chrome.chmod(0o755)
        return SimpleNamespace(returncode=0, stdout=b"<p>probe</p>")
    monkeypatch.setattr(MODULE.subprocess, "run", fake_run)
    monkeypatch.setattr("sys.argv", ["bootstrap", str(root)])
    MODULE.main()
    assert any("playwright" in x and "chromium" in x for x in calls)
    assert (root / ".browser-cache/chromium-123/chrome-linux64/chrome").is_file()
