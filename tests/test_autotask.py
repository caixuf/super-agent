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


# ---- installer into a live cron store ------------------------------------------------

sys.path.insert(0, str(REPO / "tools" / "autotask"))
import install_jobs  # noqa: E402


def _store(tmp_path, *jobs):
    path = tmp_path / "cron" / "jobs.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"jobs": list(jobs), "updated_at": "x"}), encoding="utf-8")
    return path


def _task(**overrides):
    task = {"id": "memory-budget-guard", "schedule": "0 * * * *", "collect": {"kind": "none"},
            "judge": "判 ok", "write": {"kind": "file", "path": "<profile>/state/autotask/x.json"},
            "report": {"ok": "ok ", "fail": "fail "}, "retry": {"max": 2, "backoff_seconds": 60}}
    task.update(overrides)
    return task


def test_installer_adds_job_with_placeholders_resolved(tmp_path):
    store = _store(tmp_path)
    profile = "/home/u/.hermes/profiles/p"
    rows = install_jobs.install(store, [_task()], profile_path=profile)
    assert rows == [("ADD", "memory-budget-guard", "cron 0 * * * *")]
    job = json.loads(store.read_text())["jobs"][0]
    assert profile + "/state/autotask/x.json" in job["prompt"] and "<profile>" not in job["prompt"]
    assert job["origin"] == "autotask:memory-budget-guard" and job["enabled"] is True


def test_second_install_is_idempotent(tmp_path):
    store = _store(tmp_path)
    install_jobs.install(store, [_task()], profile_path="/p")
    rows = install_jobs.install(store, [_task()], profile_path="/p")
    assert rows == [("UNCHANGED", "memory-budget-guard", "cron 0 * * * *")]
    assert len(json.loads(store.read_text())["jobs"]) == 1


def test_human_jobs_are_never_rewritten(tmp_path):
    human = {"id": "human1", "name": "memory-budget-guard", "origin": "None", "prompt": "mine"}
    store = _store(tmp_path, human)
    rows = install_jobs.install(store, [_task()], profile_path="/p")
    assert rows[0][0] == "CONFLICT"
    assert json.loads(store.read_text())["jobs"][0]["prompt"] == "mine"


def test_create_commands_use_the_official_cli_with_resolved_paths():
    commands = install_jobs.create_commands([_task(notify=False)], profile="prod",
                                            profile_path="/home/u/.hermes/profiles/prod",
                                            deliver="feishu:oc_x", workdir="/w")
    line = commands[0]
    assert "cron create" in line and "--name memory-budget-guard" in line
    assert "--deliver local" in line, "a non-notifying job must not page anyone"
    assert "/home/u/.hermes/profiles/prod/state/autotask/x.json" in line
    notifying = install_jobs.create_commands([_task(notify=True)], profile="prod",
                                             profile_path="/home/u/.hermes/profiles/prod",
                                             deliver="feishu:oc_x")
    assert "--deliver feishu:oc_x" in notifying[0]
