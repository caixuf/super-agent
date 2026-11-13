"""Core capabilities regression suite: Pi executor, 8 fixed structured receipts, and live memory/worker services."""
import importlib.util
import re
import subprocess
from pathlib import Path

from toolsets.pi_executor.driver import (
    PiResult,
    build_argv,
    parse_jsonl,
    resolve_cli_path,
    resolve_node,
    summarize_events,
)

EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")


# ---------------------------------------------------------------------------
# 1. Pi Executor (toolsets.pi_executor)
# ---------------------------------------------------------------------------

def test_pi_executor_cli_and_argv_isolation(tmp_path: Path):
    """Pi executor resolves node/cli and builds a non-shell argv array."""
    node = resolve_node()
    cli = resolve_cli_path()
    assert Path(node).exists()
    assert Path(cli).exists()
    argv = build_argv("read-only check", cli_path=cli, model="flash", provider="litellm")
    assert isinstance(argv, list)
    assert cli in argv
    assert "--mode" in argv and "json" in argv


def test_pi_executor_fail_closed_on_error_stop_reason():
    """Even with exit_code=0, stop_reason in (error, aborted, length) must yield ok=False."""
    for bad_reason in ("error", "aborted", "length"):
        raw = "\n".join([
            '{"type":"session","version":3,"id":"s1","cwd":"/tmp"}',
            '{"type":"agent_start"}',
            f'{{"type":"message_end","message":{{"role":"assistant","stopReason":"{bad_reason}","content":[{{"type":"text","text":"partial"}}]}}}}',
            '{"type":"agent_end","messages":[],"willRetry":false}',
            '{"type":"agent_settled","aborted":false}',
        ])
        events = parse_jsonl(raw)
        assert len(events) == 5
        res: PiResult = summarize_events(events, exit_code=0)
        assert res.ok is False, f"{bad_reason} must fail closed"


def test_pi_executor_success_on_settled_stop():
    """Clean stopReason=stop + agent_settled(aborted=false) + exit_code=0 yields ok=True."""
    raw = "\n".join([
        '{"type":"session","version":3,"id":"s2","cwd":"/tmp"}',
        '{"type":"agent_start"}',
        '{"type":"message_end","message":{"role":"assistant","stopReason":"stop","content":[{"type":"text","text":"verified ok"}]}}',
        '{"type":"agent_end","messages":[],"willRetry":false}',
        '{"type":"agent_settled","aborted":false}',
    ])
    events = parse_jsonl(raw)
    assert len(events) == 5
    res: PiResult = summarize_events(events, exit_code=0)
    assert res.ok is True
    assert res.settled is True
    assert "verified ok" in res.final_text


# ---------------------------------------------------------------------------
# 2. All 8 Fixed Structured Task Reply Templates (card_receipt.py)
# ---------------------------------------------------------------------------

def test_all_eight_fixed_receipt_templates():
    """All 8 fixed task receipt renderers produce clean, zero-emoji GFM Markdown."""
    path = Path("/home/caixuf/code/imfl_skills/tools/card_receipt.py")
    assert path.is_file()
    spec = importlib.util.spec_from_file_location("imfl_card_receipt", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)

    assert len(mod.FIXED_RECEIPT_TYPES) == 8
    outputs = [
        mod.build_simulation_markdown("t-1", "v2.2.8 回归", "done", 100, 100, 0, "1m20s"),
        mod.build_triage_markdown("2026-11", 10, 8, 8, 6, 2, ["缺少注入器"]),
        mod.build_failure_triage_markdown("case_001", "用例问题", "高", ["line 42 assert"], ["case_001"], "修正阈值"),
        mod.build_redteam_review_markdown("PR#12", True, [{"name": "语法守卫", "status": "PASS", "detail": "无反模式"}], [], "可合入"),
        mod.build_case_delivery_markdown("AEB需求", [{"path": "cases/aeb.imfl", "count": 4, "coverage": "正例/反例/边界"}], True),
        mod.build_replay_markdown("bag_01", "一致", "20Hz/无断流", "0.02m", [], "可归档"),
        mod.build_bench_test_markdown("上电自检", "172.100.100.88", "v2.2.8", True, "18s", ["电源稳定"], "继续功能测试"),
        mod.build_ops_health_markdown("caixuf", "HEALTHY", [{"name": "gateway", "status": "active", "detail": "200 OK"}]),
    ]
    for md in outputs:
        assert md.strip()
        assert "|" in md
        assert not EMOJI_RE.search(md), f"emoji found in receipt: {md}"


# ---------------------------------------------------------------------------
# 3. Live Memory & Worker Services Health (caixuf host)
# ---------------------------------------------------------------------------

def test_live_caixuf_memory_and_worker_services_active():
    """Verify resident memory worker, bge-m3-sidecar, and role workers are active."""
    res = subprocess.run(
        ["systemctl", "--user", "is-active",
         "hermes-gateway-caixuf.service",
         "bge-m3-sidecar.service",
         "superagent-worker-caixuf-ops.service",
         "superagent-worker-caixuf-test.service",
         "superagent-worker-caixuf-triage.service"],
        capture_output=True, text=True, timeout=5,
    )
    states = [line.strip() for line in res.stdout.splitlines() if line.strip()]
    assert states == ["active"] * 5, f"unexpected service states: {states}"
