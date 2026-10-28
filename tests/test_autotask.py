"""autotask: one five-part contract for scheduled AI tasks.

Job files are data. This test pins the contract, the safety rules (read-only collect, writes
only under state/ or skills/candidates/), the rendered prompt (a checkable verdict is
mandatory), and the adapter to the host cron format.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools" / "autotask"))
import taskfile  # noqa: E402

JOBS = sorted((REPO / "runtime" / "autotask" / "jobs").glob("*.json"))


def _job(**overrides):
    job = {"id": "x", "schedule": "0 * * * *", "collect": {"kind": "none"}, "judge": "判 ok",
           "write": {"kind": "file", "path": "<profile>/state/autotask/x.json"},
           "report": {"ok": "ok ", "fail": "fail "}, "retry": {"max": 2, "backoff_seconds": 60}}
    job.update(overrides)
    return job


def test_every_shipped_job_matches_the_contract():
    assert len(JOBS) >= 5, [p.name for p in JOBS]
    for path in JOBS:
        assert taskfile.validate(taskfile.load(path)) == [], path.name


def test_writes_stay_inside_the_two_allowed_roots():
    for bad in ("<profile>/../etc/passwd", "/tmp/x.json", "<profile>/memories/MEMORY.md", ""):
        assert taskfile.validate(_job(write={"kind": "file", "path": bad})), bad
    for good in ("<profile>/state/autotask/x.json", "<profile>/skills/candidates/2026-10-28.md"):
        assert taskfile.validate(_job(write={"kind": "file", "path": good})) == [], good


def test_collect_must_be_read_only_and_well_formed():
    assert taskfile.validate(_job(collect={"kind": "files"}))
    assert taskfile.validate(_job(collect={"kind": "command"}))
    assert taskfile.validate(_job(collect={"kind": "shell"}))
    assert taskfile.validate(_job(collect={"kind": "files", "paths": ["<profile>/memories/MEMORY.md"]})) == []


def test_schedule_and_retry_are_checked():
    assert taskfile.validate(_job(schedule="every hour"))
    assert taskfile.validate(_job(retry={"max": -1}))
    assert taskfile.validate(_job(report={"ok": ""}))


def test_rendered_prompt_requires_a_checkable_verdict_and_forbids_side_effects():
    prompt = taskfile.render(_job())
    assert "`ok <一句判据>`" in prompt and "`fail <原因>`" in prompt
    assert "不要修改代码、配置或重启服务" in prompt
    assert "最多重试 2 次" in prompt


def test_cron_adapter_keeps_name_schedule_and_prompt():
    job = taskfile.to_cron_job(_job(notify=False))
    assert set(job) == {"name", "schedule", "prompt", "notify"}
    assert job["notify"] is False and job["name"] == "x"
    json.dumps(job)  # must be writable into jobs.json
