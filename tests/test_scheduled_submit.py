"""Tests for administrator scheduled routes and CLI entrypoint tools/scheduled_submit.py."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from agentcore.pool import Pool
from plugins.durable_dispatch import (
    DispatchService,
    build_dispatch_services,
    validate_scheduled_routes,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = REPO_ROOT / "tools" / "scheduled_submit.py"


def _make_fixture(tmp_path: Path, *, deliver_completions: bool = True,
                  scheduled_routes: dict | None = None,
                  colleagues_extra: dict | None = None):
    profile_home = tmp_path / "profile_frontdesk"
    profile_home.mkdir(parents=True, exist_ok=True)

    sources = tmp_path / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    sample_file = sources / "sample.txt"
    sample_file.write_text("sample content\n", encoding="utf-8")

    analysis_queue = tmp_path / "queue_analysis"
    dev_queue = tmp_path / "queue_dev"

    if scheduled_routes is None:
        scheduled_routes = {
            "daily_triage": {
                "route": "frontdesk:channel:session_1",
                "channel": "feishu",
                "chat": "chat_daily_001",
                "thread": "thread_daily_001",
            },
            "alerts": {
                "route": "frontdesk:channel:session_alerts",
                "channel": "feishu",
                "chat": "chat_alerts_002",
            },
        }

    dev_colleague = {
        "queue_root": str(dev_queue),
        "allowed_roots": [str(sources)],
        "display_name": "Dev Specialist",
        "display_label": "Dev",
    }
    if colleagues_extra:
        dev_colleague.update(colleagues_extra)

    settings = {
        "enabled": True,
        "queue_root": str(analysis_queue),
        "allowed_roots": [str(sources)],
        "deliver_completions": deliver_completions,
        "scheduled_routes": scheduled_routes,
        "colleagues": {
            "dev": dev_colleague,
        },
    }

    config = {
        "profile": "frontdesk",
        "plugins": {
            "enabled": ["durable-dispatch"],
            "entries": {
                "durable-dispatch": {
                    "settings": settings,
                },
            },
        },
    }

    config_file = profile_home / "config.yaml"
    config_file.write_text(yaml.safe_dump(config), encoding="utf-8")

    goal_file = tmp_path / "goal.txt"
    goal_file.write_text("Execute scheduled automated verification\n", encoding="utf-8")

    return {
        "profile_home": profile_home,
        "config_file": config_file,
        "goal_file": goal_file,
        "dev_queue": dev_queue,
        "analysis_queue": analysis_queue,
        "settings": settings,
    }


def _run_cli(*args: str):
    return subprocess.run(
        [sys.executable, str(CLI_PATH), *args],
        capture_output=True,
        text=True,
    )


def test_registered_route_creates_task_with_exact_origin(tmp_path):
    env = _make_fixture(tmp_path)
    res = _run_cli(
        "--profile-home", str(env["profile_home"]),
        "--role", "dev",
        "--route", "daily_triage",
        "--goal-file", str(env["goal_file"]),
        "--idempotency-key", "key-2026-11-09-001",
    )
    assert res.returncode == 0, f"stdout: {res.stdout}, stderr: {res.stderr}"
    data = json.loads(res.stdout)
    assert data["ok"] is True
    assert data["status"] == "queued"
    assert data["deduplicated"] is False
    task_id = data["task_id"]

    pool = Pool(str(env["dev_queue"]))
    origin = pool.origin(task_id)
    assert origin == {
        "profile": "frontdesk",
        "session": "scheduled:daily_triage",
        "route": "frontdesk:channel:session_1",
        "channel": "feishu",
        "chat": "chat_daily_001",
        "thread": "thread_daily_001",
        "scheduled": True,
    }

    # Verify payload fields match standard role submit
    payload = pool.job_payload(task_id)
    assert payload["goal"] == "Execute scheduled automated verification\n"
    assert payload["sources"] == []
    assert payload["_colleague_role"] == "dev"
    assert payload["_colleague_name"] == "Dev Specialist"
    assert payload["_colleague_label"] == "Dev"
    assert "_dispatch_owner" in payload


def test_registered_route_without_thread(tmp_path):
    env = _make_fixture(tmp_path)
    res = _run_cli(
        "--profile-home", str(env["profile_home"]),
        "--role", "dev",
        "--route", "alerts",
        "--goal-file", str(env["goal_file"]),
        "--idempotency-key", "key-alerts-001",
    )
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    pool = Pool(str(env["dev_queue"]))
    origin = pool.origin(data["task_id"])
    assert "thread" not in origin
    assert origin["scheduled"] is True
    assert origin["chat"] == "chat_alerts_002"


def test_same_idempotency_key_twice_deduplicates(tmp_path):
    env = _make_fixture(tmp_path)
    first = _run_cli(
        "--profile-home", str(env["profile_home"]),
        "--role", "dev",
        "--route", "daily_triage",
        "--goal-file", str(env["goal_file"]),
        "--idempotency-key", "key-dedupe-test",
    )
    assert first.returncode == 0, first.stderr
    data_first = json.loads(first.stdout)
    assert data_first["deduplicated"] is False

    second = _run_cli(
        "--profile-home", str(env["profile_home"]),
        "--role", "dev",
        "--route", "daily_triage",
        "--goal-file", str(env["goal_file"]),
        "--idempotency-key", "key-dedupe-test",
    )
    assert second.returncode == 0, second.stderr
    data_second = json.loads(second.stdout)
    assert data_second["deduplicated"] is True
    assert data_second["task_id"] == data_first["task_id"]

    # Exactly 1 job in pool
    pool = Pool(str(env["dev_queue"]))
    with pool.db() as db:
        count = db.execute("SELECT count(*) FROM jobs").fetchone()[0]
    assert count == 1


def test_expect_new_flag(tmp_path):
    env = _make_fixture(tmp_path)
    # First invocation with --expect-new succeeds
    first = _run_cli(
        "--profile-home", str(env["profile_home"]),
        "--role", "dev",
        "--route", "daily_triage",
        "--goal-file", str(env["goal_file"]),
        "--idempotency-key", "key-expect-new",
        "--expect-new",
    )
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["deduplicated"] is False

    # Second invocation with --expect-new fails with exit code 2
    second = _run_cli(
        "--profile-home", str(env["profile_home"]),
        "--role", "dev",
        "--route", "daily_triage",
        "--goal-file", str(env["goal_file"]),
        "--idempotency-key", "key-expect-new",
        "--expect-new",
    )
    assert second.returncode == 2
    assert "expected new task" in second.stderr


def test_unregistered_route_exits_3_no_task(tmp_path):
    env = _make_fixture(tmp_path)
    res = _run_cli(
        "--profile-home", str(env["profile_home"]),
        "--role", "dev",
        "--route", "unregistered_route",
        "--goal-file", str(env["goal_file"]),
        "--idempotency-key", "key-unregistered",
    )
    assert res.returncode == 3
    assert "scheduled route not registered" in res.stderr

    pool = Pool(str(env["dev_queue"]))
    with pool.db() as db:
        count = db.execute("SELECT count(*) FROM jobs").fetchone()[0]
    assert count == 0


def test_unknown_role_exits_3(tmp_path):
    env = _make_fixture(tmp_path)
    res = _run_cli(
        "--profile-home", str(env["profile_home"]),
        "--role", "nonexistent_role",
        "--route", "daily_triage",
        "--goal-file", str(env["goal_file"]),
        "--idempotency-key", "key-unknown-role",
    )
    assert res.returncode == 3
    assert "role not configured" in res.stderr


def test_malformed_scheduled_routes_exits_2(tmp_path):
    # Route name starting with a number violates [a-z][a-z0-9_-]{0,31}
    bad_routes = {
        "1invalid_name": {
            "route": "r", "channel": "c", "chat": "ch"
        }
    }
    env = _make_fixture(tmp_path, scheduled_routes=bad_routes)
    res = _run_cli(
        "--profile-home", str(env["profile_home"]),
        "--role", "dev",
        "--route", "1invalid_name",
        "--goal-file", str(env["goal_file"]),
        "--idempotency-key", "key-bad",
    )
    assert res.returncode == 2
    assert "invalid configuration" in res.stderr or "invalid scheduled route name" in res.stderr


def test_malformed_scheduled_routes_missing_field(tmp_path):
    # Missing 'chat'
    bad_routes = {
        "missing_chat": {
            "route": "r", "channel": "c"
        }
    }
    env = _make_fixture(tmp_path, scheduled_routes=bad_routes)
    res = _run_cli(
        "--profile-home", str(env["profile_home"]),
        "--role", "dev",
        "--route", "missing_chat",
        "--goal-file", str(env["goal_file"]),
        "--idempotency-key", "key-missing-chat",
    )
    assert res.returncode == 2
    assert "invalid configuration" in res.stderr or "invalid scheduled route field" in res.stderr


def test_scheduled_routes_under_colleague_entry_rejected(tmp_path):
    # scheduled_routes placed under colleagues.dev
    env = _make_fixture(tmp_path, colleagues_extra={"scheduled_routes": {"foo": {}}})
    res = _run_cli(
        "--profile-home", str(env["profile_home"]),
        "--role", "dev",
        "--route", "daily_triage",
        "--goal-file", str(env["goal_file"]),
        "--idempotency-key", "key-colleague-rejected",
    )
    assert res.returncode == 2
    assert "invalid administrator colleague configuration" in res.stderr


def test_deliver_completions_false_refused_no_task(tmp_path):
    env = _make_fixture(tmp_path, deliver_completions=False)
    res = _run_cli(
        "--profile-home", str(env["profile_home"]),
        "--role", "dev",
        "--route", "daily_triage",
        "--goal-file", str(env["goal_file"]),
        "--idempotency-key", "key-no-delivery",
    )
    assert res.returncode == 2
    assert "deliver_completions" in res.stderr

    pool = Pool(str(env["dev_queue"]))
    with pool.db() as db:
        count = db.execute("SELECT count(*) FROM jobs").fetchone()[0]
    assert count == 0


def test_oversized_goal_file_rejected(tmp_path):
    env = _make_fixture(tmp_path)
    big_goal = tmp_path / "big_goal.txt"
    big_goal.write_text("x" * (65 * 1024), encoding="utf-8")
    res = _run_cli(
        "--profile-home", str(env["profile_home"]),
        "--role", "dev",
        "--route", "daily_triage",
        "--goal-file", str(big_goal),
        "--idempotency-key", "key-oversized",
    )
    assert res.returncode == 2
    assert "exceeds size limit" in res.stderr


def test_existing_frontdesk_durable_submit_still_rejects_session_without_route(tmp_path):
    env = _make_fixture(tmp_path)
    services = build_dispatch_services(env["settings"], "frontdesk")
    dev_service = services["dev"]

    # Calling standard submit without an addressable bound route must fail
    with pytest.raises(PermissionError, match="addressable submitting conversation required before dispatch"):
        dev_service.submit("cron_session_without_route", {
            "goal": "Run cron without addressable session",
            "request_key": "req-cron-001",
        })


def test_validate_scheduled_routes_unit():
    # Empty / None
    assert validate_scheduled_routes(None) == {}
    assert validate_scheduled_routes({}) == {}

    # Valid
    valid = {
        "route_a": {"route": "r1", "channel": "ch1", "chat": "c1"},
        "route_b": {"route": "r2", "channel": "ch2", "chat": "c2", "thread": "t2"},
    }
    res = validate_scheduled_routes(valid)
    assert res["route_a"] == {"route": "r1", "channel": "ch1", "chat": "c1"}
    assert res["route_b"]["thread"] == "t2"

    # Too many routes (> 16)
    too_many = {f"r_{i}": {"route": "r", "channel": "c", "chat": "ch"} for i in range(17)}
    with pytest.raises(ValueError, match="bounded administrator mapping"):
        validate_scheduled_routes(too_many)

    # Invalid names
    for bad_name in ("", "1abc", "Route", "bad name", "a" * 33, "a@b"):
        with pytest.raises(ValueError, match="invalid scheduled route name"):
            validate_scheduled_routes({bad_name: {"route": "r", "channel": "c", "chat": "ch"}})

    # Unknown fields
    with pytest.raises(ValueError, match="unexpected fields"):
        validate_scheduled_routes({"ok_name": {"route": "r", "channel": "c", "chat": "ch", "extra": "x"}})

    # Missing or blank required fields
    with pytest.raises(ValueError, match="invalid scheduled route field"):
        validate_scheduled_routes({"ok_name": {"route": "", "channel": "c", "chat": "ch"}})
    with pytest.raises(ValueError, match="invalid scheduled route field"):
        validate_scheduled_routes({"ok_name": {"route": "r", "channel": "  ", "chat": "ch"}})
    with pytest.raises(ValueError, match="invalid scheduled route field"):
        validate_scheduled_routes({"ok_name": {"route": "r", "channel": "c", "chat": ""}})

    # Invalid thread
    with pytest.raises(ValueError, match="invalid scheduled route field thread"):
        validate_scheduled_routes({"ok_name": {"route": "r", "channel": "c", "chat": "ch", "thread": "   "}})

    # Thread None / null handled gracefully
    res_null = validate_scheduled_routes({"ok_name": {"route": "r", "channel": "c", "chat": "ch", "thread": None}})
    assert "thread" not in res_null["ok_name"]


def test_concurrent_submit_same_idempotency_key(tmp_path):
    import concurrent.futures
    env = _make_fixture(tmp_path)
    services = build_dispatch_services(env["settings"], "frontdesk")
    dev_service = services["dev"]

    results = []
    def _submit(idx):
        return dev_service.submit_scheduled(
            "Execute concurrent verification\n",
            route_name="daily_triage",
            idempotency_key="shared-race-key-001",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(_submit, i) for i in range(10)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    task_ids = {r["task_id"] for r in results}
    assert len(task_ids) == 1, f"expected 1 task_id, got: {task_ids}"

    new_count = sum(1 for r in results if not r["deduplicated"])
    dedup_count = sum(1 for r in results if r["deduplicated"])
    assert new_count == 1, f"expected exactly 1 new submission, got {new_count}"
    assert dedup_count == 9, f"expected exactly 9 deduped submissions, got {dedup_count}"

    pool = Pool(str(env["dev_queue"]))
    with pool.db() as db:
        count = db.execute("SELECT count(*) FROM jobs").fetchone()[0]
    assert count == 1


def test_concurrent_cli_processes_expect_new(tmp_path):
    import concurrent.futures
    env = _make_fixture(tmp_path)

    def _call():
        return _run_cli(
            "--profile-home", str(env["profile_home"]),
            "--role", "dev",
            "--route", "daily_triage",
            "--goal-file", str(env["goal_file"]),
            "--idempotency-key", "race-expect-new-key",
            "--expect-new",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(_call)
        f2 = ex.submit(_call)
        res1 = f1.result()
        res2 = f2.result()

    returncodes = sorted([res1.returncode, res2.returncode])
    assert returncodes == [0, 2], f"expected [0, 2], got: {returncodes}"


def test_scheduled_task_status_and_cancel_by_route(tmp_path):
    env = _make_fixture(tmp_path)
    session_routes = {
        "caller_same_route": {
            "route": "frontdesk:channel:session_1",
            "channel": "feishu",
            "chat": "chat_daily_001",
        },
        "caller_other_route": {
            "route": "frontdesk:channel:session_diff",
            "channel": "feishu",
            "chat": "chat_diff_999",
        },
        "caller_no_route": {},
    }

    def route_provider(sid):
        return session_routes.get(sid, {})

    services = build_dispatch_services(env["settings"], "frontdesk", route_provider=route_provider)
    dev_service = services["dev"]

    # 1. Submit scheduled task for daily_triage (route: "frontdesk:channel:session_1")
    res = dev_service.submit_scheduled(
        "Execute daily triage\n",
        route_name="daily_triage",
        idempotency_key="key-sched-status-01",
    )
    sched_job = res["task_id"]
    assert res["status"] == "queued"

    # Same-route caller can query status
    status_res = dev_service.status("caller_same_route", {"task_id": sched_job})
    assert status_res["ok"] is True
    assert status_res["task_id"] == sched_job
    assert status_res["status"] == "queued"
    assert status_res["cancellation_requested"] is False

    # Different-route caller is refused status
    with pytest.raises(PermissionError, match="task unavailable in this session"):
        dev_service.status("caller_other_route", {"task_id": sched_job})

    # Caller with no route is refused status
    with pytest.raises(PermissionError, match="task unavailable in this session"):
        dev_service.status("caller_no_route", {"task_id": sched_job})

    # Foreign profile caller with matching route is refused status and cancel
    foreign_service = DispatchService(
        str(env["dev_queue"]),
        "foreign_profile",
        env["settings"]["allowed_roots"],
        role="dev",
        route_provider=route_provider,
    )
    with pytest.raises(PermissionError, match="task unavailable in this session"):
        foreign_service.status("caller_same_route", {"task_id": sched_job})
    with pytest.raises(PermissionError, match="task unavailable in this session"):
        foreign_service.cancel("caller_same_route", {"task_id": sched_job})

    # Different-route caller cannot cancel
    with pytest.raises(PermissionError, match="task unavailable in this session"):
        dev_service.cancel("caller_other_route", {"task_id": sched_job})

    # Caller with no route cannot cancel
    with pytest.raises(PermissionError, match="task unavailable in this session"):
        dev_service.cancel("caller_no_route", {"task_id": sched_job})

    # Same-route caller can cancel
    cancel_res = dev_service.cancel("caller_same_route", {"task_id": sched_job})
    assert cancel_res["ok"] is True
    assert cancel_res["task_id"] == sched_job
    assert cancel_res["cancellation_requested"] is True
    assert cancel_res["status"] == "queued"

    # Status after cancel shows cancellation_requested=True
    status_after = dev_service.status("caller_same_route", {"task_id": sched_job})
    assert status_after["status"] == "queued"
    assert status_after["cancellation_requested"] is True


def test_non_scheduled_task_unaffected_by_same_route(tmp_path):
    env = _make_fixture(tmp_path)
    # Both alice and bob are in the same chat/route
    session_routes = {
        "alice_session": {
            "route": "frontdesk:channel:session_1",
            "channel": "feishu",
            "chat": "chat_daily_001",
        },
        "bob_session": {
            "route": "frontdesk:channel:session_1",
            "channel": "feishu",
            "chat": "chat_daily_001",
        },
    }

    def route_provider(sid):
        return session_routes.get(sid, {})

    services = build_dispatch_services(env["settings"], "frontdesk", route_provider=route_provider)
    dev_service = services["dev"]

    # Alice submits a regular (non-scheduled) task
    submit_res = dev_service.submit("alice_session", {
        "goal": "Regular task from alice\n",
        "request_key": "req-alice-01",
    })
    normal_job = submit_res["task_id"]

    # Alice can query status and cancel her own task
    alice_status = dev_service.status("alice_session", {"task_id": normal_job})
    assert alice_status["ok"] is True
    assert alice_status["task_id"] == normal_job

    # Bob has the EXACT same route, but because the task is non-scheduled,
    # Bob is refused access
    with pytest.raises(PermissionError, match="task unavailable in this session"):
        dev_service.status("bob_session", {"task_id": normal_job})

    with pytest.raises(PermissionError, match="task unavailable in this session"):
        dev_service.cancel("bob_session", {"task_id": normal_job})

    # Alice can cancel her own task
    cancel_res = dev_service.cancel("alice_session", {"task_id": normal_job})
    assert cancel_res["ok"] is True
    assert cancel_res["cancellation_requested"] is True


def test_scheduled_task_status_and_cancel_via_tools(tmp_path):
    from plugins.durable_dispatch import register
    import plugins.durable_dispatch as dd_mod

    env = _make_fixture(tmp_path)
    session_routes = {
        "chat_user_session": {
            "route": "frontdesk:channel:session_1",
            "channel": "feishu",
            "chat": "chat_daily_001",
        },
        "outsider_session": {
            "route": "frontdesk:channel:other_route",
            "channel": "feishu",
            "chat": "chat_other",
        },
    }

    class ContextStub:
        def __init__(self, settings):
            self.settings = settings
            self.profile_name = "frontdesk"
            self.tools = {}
            self.prompt_sections = {}

        def get_config(self, key, default=None):
            return self.settings.get(key, default)

        def register_tool(self, **kwargs):
            self.tools[kwargs["name"]] = kwargs

        def register_system_prompt_section(self, id, content, **kwargs):
            self.prompt_sections[id] = {"content": content, **kwargs}

    orig_bound_route = dd_mod._bound_route
    try:
        dd_mod._bound_route = lambda sid: session_routes.get(sid, {})
        ctx = ContextStub(env["settings"])
        register(ctx)

        services = build_dispatch_services(env["settings"], "frontdesk", route_provider=dd_mod._bound_route)
        dev_service = services["dev"]
        res = dev_service.submit_scheduled(
            "Scheduled work for chat user\n",
            route_name="daily_triage",
            idempotency_key="key-sched-tool-01",
        )
        task_id = res["task_id"]

        status_handler = ctx.tools["durable_status"]["handler"]
        cancel_handler = ctx.tools["durable_cancel"]["handler"]

        # Outsider cannot query status
        raw_out_status = status_handler({"task_id": task_id}, session_id="outsider_session")
        out_status = json.loads(raw_out_status)
        assert out_status["ok"] is False
        assert "task unavailable in this session" in out_status["error"]

        # Chat user on same route CAN query status
        raw_user_status = status_handler({"task_id": task_id}, session_id="chat_user_session")
        user_status = json.loads(raw_user_status)
        assert user_status["ok"] is True
        assert user_status["task_id"] == task_id
        assert user_status["status"] == "queued"

        # Outsider cannot cancel
        raw_out_cancel = cancel_handler({"task_id": task_id}, session_id="outsider_session")
        out_cancel = json.loads(raw_out_cancel)
        assert out_cancel["ok"] is False
        assert "task unavailable in this session" in out_cancel["error"]

        # Chat user on same route CAN cancel
        raw_user_cancel = cancel_handler({"task_id": task_id}, session_id="chat_user_session")
        user_cancel = json.loads(raw_user_cancel)
        assert user_cancel["ok"] is True
        assert user_cancel["task_id"] == task_id
        assert user_cancel["cancellation_requested"] is True
    finally:
        dd_mod._bound_route = orig_bound_route


