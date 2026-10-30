"""Generic templates ship in a shape a deployment can copy verbatim.

Cron templates: valid JSON, one job per file, cron syntax, an explicit read-only or
notify decision, and no business literals. Skills: every ``skills/<name>`` is installable
by tools/install_skills.py (frontmatter name matches the directory, description says when
to use it), and examples stay templates.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CRON = REPO / "runtime" / "cron"
SKILLS = REPO / "skills"
CRON_FIELD = re.compile(r"^[\d*/,\- ]+$")
def _cron_templates():
    return sorted(CRON.glob("*.json"))


def test_cron_templates_are_valid_and_complete():
    templates = _cron_templates()
    assert templates, "runtime/cron has no templates"
    for path in templates:
        jobs = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(jobs, list) and jobs, path.name
        for job in jobs:
            assert {"name", "schedule", "prompt"} <= set(job), (path.name, job.get("name"))
            assert CRON_FIELD.match(job["schedule"]), (path.name, job["schedule"])
            assert len(job["schedule"].split()) == 5, (path.name, job["schedule"])
            assert isinstance(job.get("notify"), bool), (path.name, job["name"])
            assert job["prompt"].strip(), (path.name, job["name"])


def test_cron_templates_stay_read_only_or_explicit():
    """A template must not tell the bot to change code, config, or restart services."""
    forbidden = ("systemctl restart", "systemctl stop", "rm -rf", "git push")
    for path in _cron_templates():
        for job in json.loads(path.read_text(encoding="utf-8")):
            prompt = job["prompt"]
            for word in forbidden:
                assert word not in prompt, (path.name, job["name"], word)
            assert "只读" in prompt or "不要改" in prompt, (path.name, job["name"])


def _skill_dirs():
    return sorted(p for p in SKILLS.iterdir() if p.is_dir() and p.name != "examples")


def test_each_generic_skill_declares_its_name_and_when_to_use_it():
    skills = _skill_dirs()
    assert len(skills) >= 3, [p.name for p in skills]
    for skill in skills:
        text = (skill / "SKILL.md").read_text(encoding="utf-8")
        assert text.startswith("---"), skill.name
        front = text.split("---")[1]
        name = re.search(r"(?m)^name:\s*(\S+)\s*$", front)
        description = re.search(r"(?m)^description:\s*(.+)$", front)
        assert name and name.group(1) == skill.name, (skill.name, name and name.group(1))
        assert description and re.search(r"Use (when|for)", description.group(1)), skill.name


def test_examples_are_not_installable_skills():
    installable = {p.name for p in _skill_dirs()}
    examples = {p.name for p in (SKILLS / "examples").iterdir() if p.is_dir()}
    assert examples, "skills/examples is empty"
    assert not (examples & installable)
