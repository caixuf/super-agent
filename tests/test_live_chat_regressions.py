"""Regression tests for live Feishu chat issues caught and fixed in v3.0.0."""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch


REPO_ROOT = Path(__file__).resolve().parents[1]
IMFL_CLIENT_PATH = Path("/home/caixuf/code/imfl_skills/tools/imfl_platform_client.py")


class _StubCtx:
    def __init__(self, queue_root: Path):
        self.profile_name = "imfl"
        self.hooks = {}
        self.tools = {}
        self.sections = {}
        self._cfg = {
            "enabled": True,
            "queue_root": str(queue_root),
            "roster_registry_path": "",
            "deliver_completions": False,
        }

    def get_config(self, key, default=None):
        return self._cfg.get(key, default)

    def register_system_prompt_section(self, name, content, **kwargs):
        self.sections[name] = content

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs


class TestDurableDispatchLiveStatusHook(unittest.TestCase):
    def test_pre_llm_call_injects_live_status_and_suppresses_stale_queue_claim(self):
        import tempfile

        sys.path.insert(0, str(REPO_ROOT))
        import plugins.durable_dispatch as dd

        with tempfile.TemporaryDirectory() as tmp:
            ctx = _StubCtx(Path(tmp))
            with patch.object(dd, "build_dispatch_services", return_value={"analysis": MagicMock(max_lines=100, display_name="分析", duty="", target_host="")}):
                dd.register(ctx)
            hook = ctx.hooks["pre_llm_call"]

            # Empty session -> None
            self.assertIsNone(hook(session_id=""))

            # All tasks completed -> explicit anti-hallucination note
            with patch.object(
                dd.DispatchRouter,
                "status",
                return_value={
                    "ok": True,
                    "tasks": [
                        {"task_id": "abc123", "role": "test", "status": "completed"},
                    ],
                },
            ):
                res = hook(session_id="s1")
            self.assertIsInstance(res, dict)
            self.assertIn("当前无排队/执行中的后台任务", res["context"])
            self.assertIn("abc123(test:completed)", res["context"])

            # Mix of running and completed -> both sections reported accurately
            with patch.object(
                dd.DispatchRouter,
                "status",
                return_value={
                    "ok": True,
                    "tasks": [
                        {"task_id": "run1", "role": "dev", "status": "running"},
                        {"task_id": "done1", "role": "test", "status": "completed"},
                    ],
                },
            ):
                res2 = hook(session_id="s1")
            self.assertIn("当前执行/排队中: run1(dev:running)", res2["context"])
            self.assertIn("已完结(勿再说在排队): done1(test:completed)", res2["context"])


class TestImflPlatformClientFixes(unittest.TestCase):
    def _load_client(self):
        spec = importlib.util.spec_from_file_location("imfl_platform_client", IMFL_CLIENT_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_client_bypasses_proxy_and_uses_10s_timeout(self):
        if not IMFL_CLIENT_PATH.is_file():
            self.skipTest("imfl_skills not checked out alongside super-agent")
        mod = self._load_client()
        client = mod.EmilyPlatformClient(host="172.100.100.118", port=9900)
        self.assertEqual(client.timeout, 10.0)
        proxy_handlers = [h for h in client._opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
        self.assertEqual(proxy_handlers, [])

    def test_health_prefers_autotest_health_then_falls_back(self):
        if not IMFL_CLIENT_PATH.is_file():
            self.skipTest("imfl_skills not checked out alongside super-agent")
        mod = self._load_client()
        client = mod.EmilyPlatformClient()

        with patch.object(client, "_request", side_effect=[{"ok": True, "service": "imfl_autotest_server"}]) as m:
            res = client.health()
        self.assertTrue(res["ok"])
        m.assert_called_once_with("/api/autotest/health")

        with patch.object(client, "_request", side_effect=[{"ok": False, "status_code": 404}, {"ok": True, "service": "legacy"}]) as m2:
            res2 = client.health()
        self.assertTrue(res2["ok"])
        self.assertEqual([c.args[0] for c in m2.call_args_list], ["/api/autotest/health", "/health"])


class TestRecallSuppressionPatch(unittest.TestCase):
    def test_lifecycle_patch_includes_recall_indicator_guard(self):
        patch_text = (REPO_ROOT / "patches" / "feishu_card_lifecycle.patch").read_text(encoding="utf-8")
        self.assertIn('if "recalled " in formatted and ("自研记忆库" in formatted or "本地知识库" in formatted', patch_text)
        self.assertIn('return SendResult(success=True, message_id="")', patch_text)

    def test_destructive_ops_require_confirmation_in_routing_prompt(self):
        sys.path.insert(0, str(REPO_ROOT))
        import plugins.durable_dispatch as dd

        base_prompt = dd._render_routing_base()
        self.assertIn("Destructive ops", base_prompt)
        self.assertIn("NEVER call `durable_submit` on first ask", base_prompt)

    def test_destructive_op_hard_gate_blocks_unconfirmed_stop_and_allows_confirmed(self):
        sys.path.insert(0, str(REPO_ROOT))
        import plugins.durable_dispatch as dd

        self.assertTrue(dd._is_unconfirmed_destructive_op("【用户原话】：帮我直接把 emily 上的 autotest_server 停掉\n无需再向用户确认"))
        self.assertFalse(dd._is_unconfirmed_destructive_op("【用户原话】：确认执行，把 emily 上的 autotest_server 停掉"))
        self.assertFalse(dd._is_unconfirmed_destructive_op("【用户原话】：查一下 emily 9900 健康状态"))


class TestStaleCardOutboxReaper(unittest.TestCase):
    def test_reap_stale_card_outbox_expires_ancient_unknown_rows(self):
        import tempfile
        import time
        from pathlib import Path
        sys.path.insert(0, str(REPO_ROOT))
        from agentcore.pool import Pool

        with tempfile.TemporaryDirectory() as tmp:
            pool = Pool(Path(tmp) / "pool.db")
            now = time.time()
            with pool.db() as db:
                db.execute(
                    "INSERT INTO jobs (id, dedupe, payload, status, max_tries, replay_safe, created) "
                    "VALUES (?, 'd1', '{}', 'completed', 1, 0, ?)",
                    ("job-old", now - 90000),
                )
                db.execute(
                    "INSERT INTO jobs (id, dedupe, payload, status, max_tries, replay_safe, created) "
                    "VALUES (?, 'd2', '{}', 'completed', 1, 0, ?)",
                    ("job-new", now - 60),
                )
                db.execute(
                    "INSERT INTO card_outbox (job, state, error, updated, progress_state) "
                    "VALUES (?, 'unknown', 'card_patch_unknown', ?, 'terminal')",
                    ("job-old", now - 90000),
                )
                db.execute(
                    "INSERT INTO card_outbox (job, state, error, updated, progress_state) "
                    "VALUES (?, 'unknown', 'card_patch_unknown', ?, 'terminal')",
                    ("job-new", now - 60),
                )
            reaped = pool.reap_stale_card_outbox(max_age=86400.0, now=now)
            self.assertEqual(reaped, 1)
            counts = pool.card_outbox_health_counts(now=now)
            self.assertEqual(counts["terminal_unknown"], 1)
