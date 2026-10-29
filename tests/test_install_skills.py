"""tools/install_skills.py: installs generic skills, never examples, never clobbers edits."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "tools" / "install_skills.py"


def _run(home: Path, *extra):
    env = dict(os.environ, HOME=str(home))
    return subprocess.run([sys.executable, str(TOOL), *extra], env=env,
                          capture_output=True, text=True, check=False)


def test_installs_generic_skills_and_skips_examples(tmp_path):
    home = tmp_path / "home"
    (home / ".hermes" / "profiles" / "p" / "skills").mkdir(parents=True)
    proc = _run(home, "--profile", "p")
    assert proc.returncode == 0, proc.stderr
    skills = home / ".hermes" / "profiles" / "p" / "skills"
    assert (skills / "research-before-build" / "SKILL.md").is_file()
    assert (skills / "illustrated-doc-rendering" / "SKILL.md").is_file()
    assert not (skills / "examples").exists(), "examples are templates, never installed"

    again = _run(home, "--profile", "p")
    assert "UNCHANGED" in again.stdout and "INSTALL" not in again.stdout


def test_local_edit_is_reported_not_overwritten(tmp_path):
    home = tmp_path / "home"
    target = home / ".hermes" / "profiles" / "p" / "skills" / "research-before-build"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("locally improved\n", encoding="utf-8")

    proc = _run(home, "--profile", "p", "--only", "research-before-build")

    assert "CONFLICT" in proc.stdout
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "locally improved\n"


def test_dry_run_writes_nothing(tmp_path):
    home = tmp_path / "home"
    proc = _run(home, "--profile", "p", "--dry-run")
    assert "INSTALL" in proc.stdout
    assert not (home / ".hermes" / "profiles" / "p" / "skills").exists()
