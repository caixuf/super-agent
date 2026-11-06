"""Tests for /boost acceleration, 100+ dynamic slots and queue latency optimizations."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import threading
import time
import pytest

from agentcore.colleague_pool import ColleaguePool, MorphedColleagueContext
from agentcore.pool import Pool
from agentcore.worker import Worker
from plugins.durable_dispatch import (
    _delivery_interval, _DEFAULT_DELIVERY_INTERVAL, format_boost_status, register,
)
from runtime.hermes_frontdesk_policy import command_block_message, CHAT_COMMANDS


def test_colleague_pool_default_100_plus_concurrency_slots(tmp_path):
    """Verify ColleaguePool defaults to 100+ (128) homogeneous dynamic slots."""
    pool = ColleaguePool(scratch_root=tmp_path / "scratch")
    assert pool.max_workers >= 100
    assert pool.max_concurrent >= 100
    assert pool.idle_count >= 100
    assert pool.capacity >= 200
    pool.close()


def test_colleague_pool_128_slots_parallel_throughput(tmp_path):
    """Verify 128 parallel slots can be acquired and executed concurrently without stampede."""
    slots_count = 128
    pool = ColleaguePool(max_workers=slots_count, scratch_root=tmp_path / "scratch_128")
    roles = ["architect", "cases", "reviewer", "test", "triage", "maintainer", "ops", "dev", "replay"]

    def quick_task(ctx: MorphedColleagueContext):
        f = f"result_{ctx.slot_id}.txt"
        ctx.write_file(f, f"Slot {ctx.slot_id} morphed into {ctx.role}")
        assert ctx.read_file(f).startswith("Slot ")
        return {"slot": ctx.slot_id, "role": ctx.role}

    t0 = time.monotonic()
    handles = []
    for i in range(slots_count):
        r = roles[i % len(roles)]
        h = pool.submit_colleague_task(role=r, goal=f"Boost task {i}", handler=quick_task)
        handles.append(h)

    for h in handles:
        res = h.result(timeout=10.0)
        assert res["ok"] is True

    elapsed = time.monotonic() - t0
    assert pool.completed_count == slots_count
    # Event-driven execution across 128 slots completes rapidly
    assert elapsed < 5.0
    pool.close()


def test_worker_poll_interval_sub_second_latency(tmp_path):
    """Verify Worker operates with low latency (0.05s poll interval)."""
    pool = Pool(tmp_path / "queue")
    runner_path = tmp_path / "runner.py"
    runner_path.write_text(
        "import json, sys\n"
        "job = json.load(sys.stdin)\n"
        "print(json.dumps({'ok': True, 'result': 'fast'}))\n"
    )
    cmd = [sys.executable, str(runner_path)]

    worker = Worker(pool, cmd, workers=4, poll_interval=0.05)
    assert worker.poll_interval == 0.05

    # Submit 5 jobs
    for i in range(5):
        pool.submit(f"fast-job-{i}", {"index": i})

    t0 = time.monotonic()
    stats = worker.run(drain=True)
    elapsed = time.monotonic() - t0

    assert stats["attempts_finished"] == 5
    assert elapsed < 3.0  # Finished fast


def test_pool_cli_supports_poll_interval_flag(tmp_path):
    """Verify pool CLI parser accepts --poll-interval."""
    import subprocess
    cmd = [
        sys.executable, "-m", "agentcore.pool",
        "--root", str(tmp_path / "queue"),
        "worker", "--help"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0
    assert "--poll-interval" in res.stdout


def test_frontdesk_authorizes_boost_chat_command(tmp_path):
    """Verify frontdesk policy authorizes the 'boost' slash command."""
    (tmp_path / "config.yaml").write_text(json.dumps({
        "superagent": {"frontdesk": {"enabled": True}}
    }))
    assert "boost" in CHAT_COMMANDS
    assert command_block_message("boost", home=tmp_path) is None


def test_durable_dispatch_accelerated_delivery_interval():
    """Verify delivery interval allows sub-second intervals and has fast default."""
    assert _DEFAULT_DELIVERY_INTERVAL <= 1.0

    class MockContext:
        def __init__(self, val):
            self._val = val
        def get_config(self, key, default):
            return self._val

    assert _delivery_interval(MockContext(0.1)) == 0.1
    assert _delivery_interval(MockContext(0.05)) == 0.05
    with pytest.raises(ValueError):
        _delivery_interval(MockContext(0.01))


def test_format_boost_status_content():
    """Verify format_boost_status returns formatted markdown with 100+ slots and 50ms metrics."""
    text = format_boost_status()
    assert "Super Agent 加速执行状态报告" in text
    assert "50ms" in text
    assert "20ms" in text
    assert "1.00s" in text
    assert "128" in text
    assert "120" in text
    assert "cases" in text
    assert "dev" in text
    assert "test" in text
    assert "FlowCoro" in text


def test_durable_dispatch_registers_boost_command(tmp_path):
    """Verify durable_dispatch plugin registers /boost command with proper handler."""
    commands = {}

    class MockContext:
        profile_name = "test_profile"

        def get_config(self, key, default=None):
            cfg = {
                "enabled": True,
                "engine_root": str(tmp_path),
                "queue_root": str(tmp_path / "queue"),
                "allowed_roots": [str(tmp_path)],
                "deliver_completions": False,
            }
            return cfg.get(key, default)

        def register_tool(self, **kwargs):
            pass

        def register_system_prompt_section(self, *args, **kwargs):
            pass

        def register_command(self, name, handler, description="", args_hint=""):
            commands[name] = {"handler": handler, "description": description, "args_hint": args_hint}

    (tmp_path / "queue").mkdir(parents=True, exist_ok=True)
    ctx = MockContext()
    register(ctx)

    assert "boost" in commands
    assert "100+" in commands["boost"]["description"] or "并发槽位" in commands["boost"]["description"]
    
    # Execute the registered handler
    output = commands["boost"]["handler"]("")
    assert isinstance(output, str)
    assert "Super Agent 加速执行状态报告" in output
    assert "50ms" in output
    assert "128" in output
