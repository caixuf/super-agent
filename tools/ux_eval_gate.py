#!/usr/bin/env python3
"""Unified P0-P2 UX, Card Lifecycle, and Private-Domain Golden Eval Gate."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _check_p0_card_patch(hermes_dir: Path) -> dict:
    """Verify feishu_card_lifecycle.patch is applied and never drops final replies."""
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "patches" / "apply_feishu_card_lifecycle.py"), "--check"],
        env=dict(os.environ, HERMES_AGENT_DIR=str(hermes_dir)),
        capture_output=True,
        text=True,
        timeout=15,
    )
    adapter_py = hermes_dir / "plugins" / "platforms" / "feishu" / "adapter.py"
    has_fix = False
    if adapter_py.is_file():
        text = adapter_py.read_text(encoding="utf-8", errors="replace")
        has_fix = (
            'bool((metadata or {}).get("_sa_status"))' in text
            and 'error_kind="sa_card_update_unresolved"' in text
        )
    ok = proc.returncode == 0 and has_fix
    return {
        "name": "P0.1 Feishu Card Ownership Fix",
        "ok": ok,
        "detail": f"patch_check={proc.returncode}, final_reply_guard={has_fix}",
    }


def _check_p0_silent_subtask_and_compaction() -> dict:
    """Verify silent subtask card suppression, parent aggregation, and <=800-char compaction."""
    from agentcore.pool import Pool
    from install.worker.dev_exec_runner import _compact_summary
    from plugins.durable_dispatch import DispatchService

    with tempfile.TemporaryDirectory() as td:
        q_root = Path(td) / "queue"
        svc = DispatchService(
            str(q_root),
            "demo",
            ["/"],
            role="triage",
            display_name="分诊",
            task_cards_enabled=True,
            route_provider=lambda sid: {
                "route": "agent:demo:feishu:group:oc_eval",
                "channel": "feishu",
                "chat": "oc_eval",
            },
        )
        parent = svc.submit("sess-eval", {"goal": "parent", "request_key": "p-eval"})
        sub = svc.submit(
            "sess-eval",
            {"goal": "sub", "request_key": "s-eval", "parent_task_id": parent["task_id"]},
        )
        pool = Pool(q_root)
        pending = [r["job"] for r in pool.pending_card_creations()]
        ok_cards = (
            parent.get("ok") is True
            and sub.get("silent") is True
            and (parent["task_id"] in pending)
            and (sub["task_id"] not in pending)
        )

    long_text = "结论：已完成回归验证。\n\n| 项 | 结果 |\n| :--- | :--- |\n| 单测 | PASS |\n\n" + ("冗余细节。" * 250)
    compacted = _compact_summary(long_text, [str(Path(td) / "full_report.md")], limit=800)
    ok_len = len(compacted) <= 800 and "结论：" in compacted and "full_report.md" in compacted
    return {
        "name": "P0.2 Silent Subtask & 800-Char Compaction",
        "ok": bool(ok_cards and ok_len),
        "detail": f"parent_only_card={ok_cards}, compacted_len={len(compacted)}",
    }


def _check_p1_profile_ux_and_progress(profile_home: Path) -> dict:
    """Verify profile UX flags (card_replies, streaming, tool_progress, show_commentary) & progress bar."""
    from agentcore.task_card_bridge import TaskCardBridge

    cfg_path = profile_home / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    feishu_plat = (cfg.get("platforms") or {}).get("feishu") or {}
    feishu_disp = ((cfg.get("display") or {}).get("platforms") or {}).get("feishu") or {}
    flags = {
        "card_replies": feishu_plat.get("card_replies") is True,
        "streaming": feishu_disp.get("streaming") is True,
        "tool_progress": feishu_disp.get("tool_progress") is True,
        "show_commentary": (cfg.get("display") or {}).get("show_commentary") is True,
    }
    with tempfile.TemporaryDirectory() as td:
        bridge = TaskCardBridge(Path(td) / "cards", profile="eval", min_update_interval=0.0)
        bridge.bind_task("t-p1", origin={"route": "agent:demo:feishu:group:oc_eval"}, goal="P1 progress check", role="dev", display_name="研发执行")
        prog = bridge.update_progress("t-p1", status="running", phase="正在执行工具: pytest", progress=0.65, elapsed=4.2)
        card_json = json.dumps(prog.get("card_payload") or {}, ensure_ascii=False)
        ok_prog = prog.get("ok") is True and "65%" in card_json and "正在执行工具: pytest" in card_json
    ok = all(flags.values()) and ok_prog
    return {
        "name": "P1 Profile UX Flags & Milestone Progress",
        "ok": ok,
        "detail": f"flags={flags}, progress_card={ok_prog}",
    }


def _check_p2_private_domain_eval(profile_name: str, sa_root: Path) -> dict:
    """Run private-domain recall + 100-concurrency pool benchmark."""
    from agentcore.colleague_pool import ColleaguePool

    data_dir = sa_root / "state" / "personal-memory-candidate" / "data"
    entity_db = sa_root / "state" / "entity-rules-20261104" / profile_name / "entity_rules.db"
    t0 = time.time()
    recall_ms = None
    recalled_count = 0
    recall_ok = False
    if data_dir.is_dir() and entity_db.is_file():
        cmd = [
            sys.executable,
            str(REPO_ROOT / "tools" / "recall_combined.py"),
            "--query=caixuf WSL2 环境与实车安全准则",
            "--entity-db", str(entity_db),
            "--data-dir", str(data_dir),
            "--user", profile_name,
            "--agent", f"{profile_name}-hermes",
            "--profile", profile_name,
            "--embed-url", "http://127.0.0.1:8910",
            "--limit", "5",
        ]
        res = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=20)
        recall_ms = round((time.time() - t0) * 1000, 1)
        if res.returncode == 0:
            payload = json.loads(res.stdout)
            recalled_count = len(payload.get("results", []))
            recall_ok = recalled_count >= 1

    pool = ColleaguePool(max_workers=20, capacity=200, backend="stdlib")
    t1 = time.time()
    handles = [
        pool.submit_colleague_task(
            role=["reviewer", "cases", "architect", "test"][i % 4],
            goal="eval_ping",
            payload={"idx": i},
            handler=lambda ctx, idx=i: idx,
        )
        for i in range(100)
    ]
    results = [h.result(timeout=10.0) for h in handles]
    pool_s = round(time.time() - t1, 3)
    pool.close()
    pool_ok = len(results) == 100
    return {
        "name": "P2 Private-Domain Recall & Pool Concurrency",
        "ok": bool(recall_ok and pool_ok),
        "detail": f"recall_hits={recalled_count} ({recall_ms}ms), pool_100={pool_s}s",
    }


def _check_runtime_outbox_health(sa_root: Path) -> dict:
    """Check runtime outbox DBs for stuck unknown/degraded cards."""
    stuck = []
    for db in sorted((sa_root / "state").glob("**/pool.db")):
        try:
            conn = sqlite3.connect(str(db))
            rows = conn.execute(
                "SELECT task_id, state, last_error FROM card_outbox WHERE state NOT IN ('delivered', 'completed')"
            ).fetchall()
            for r in rows:
                stuck.append((db.parent.name, r[0], r[1], r[2]))
            conn.close()
        except sqlite3.Error:
            pass
    return {
        "name": "Runtime Card Outbox Health",
        "ok": len(stuck) == 0,
        "detail": f"stuck_cards={len(stuck)}" + (f" ({stuck[:3]})" if stuck else ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="caixuf")
    parser.add_argument("--send-group", action="store_true", help="Send CardKit 2.0 report to test group")
    parser.add_argument("--chat-id", default=os.environ.get("LIVE_TEST_CHAT_ID", "oc_c06389ec97acfce211a3fbbbe37a467c"))
    args = parser.parse_args()

    profile_home = Path.home() / ".hermes" / "profiles" / args.profile
    sa_root = Path.home() / "super_agent" / args.profile
    hermes_dir = sa_root / "hermes-current"
    if not hermes_dir.exists():
        hermes_dir = Path.home() / ".hermes" / "hermes-agent"

    checks = [
        _check_p0_card_patch(hermes_dir),
        _check_p0_silent_subtask_and_compaction(),
        _check_p1_profile_ux_and_progress(profile_home),
        _check_p2_private_domain_eval(args.profile, sa_root),
        _check_runtime_outbox_health(sa_root),
    ]
    all_ok = all(c["ok"] for c in checks)
    report = {
        "profile": args.profile,
        "overall": "PASS" if all_ok else "FAIL",
        "checks": checks,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if args.send_group:
        from agentcore.cardkit2_renderer import build_cardkit2_payload
        from tools.live_group_test import get_tenant_access_token, load_feishu_credentials, send_card_message

        os.environ["HERMES_ENV_PATH"] = str(profile_home / ".env")
        os.environ["LIVE_TEST_CHAT_ID"] = args.chat_id
        app_id, app_secret = load_feishu_credentials()
        token = get_tenant_access_token(app_id, app_secret)

        lines = [
            f"结论：P0-P2 全链路门禁实测 **{'PASS' if all_ok else 'FAIL'}**（profile=`{args.profile}`）。\n",
            "| 检查项 | 状态 | 关键指标 |",
            "| :--- | :--- | :--- |",
        ]
        for c in checks:
            lines.append(f"| {c['name']} | {'PASS' if c['ok'] else 'FAIL'} | {c['detail']} |")
        card = build_cardkit2_payload(
            "\n".join(lines),
            is_terminal=True,
            is_failed=not all_ok,
            title=f"{args.profile} · P0-P2 交互与私有领域门禁实测",
            subtitle="Super Agent UX & Eval Gate",
            icon_token="myai_colorful",
            status_suffix="验收通过" if all_ok else "存在阻塞",
            footer_notes=[f"状态: {'PASS' if all_ok else 'FAIL'}", f"目标群: {args.chat_id}"],
        )
        res = send_card_message(token, card)
        print(f"Feishu group card sent: code={res.get('code')} msg_id={(res.get('data') or {}).get('message_id')}")

    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
