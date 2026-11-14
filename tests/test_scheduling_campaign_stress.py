"""Adversarial stress & regression suite for Super Agent scheduling subsystem (调度专题)."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import pytest
import yaml

from agentcore.lan_cluster import probe_node
from agentcore.pool import Pool
from agentcore.task_card_bridge import TaskCardBridge
from runtime.hermes_completion_transport import create_transport

REPO = Path(__file__).resolve().parents[1]
PLUGIN_PATH = REPO / "plugins" / "durable_dispatch" / "__init__.py"
SUPERAGENT_PATH = REPO / "tools" / "superagent.py"

_dd_spec = importlib.util.spec_from_file_location("durable_dispatch_stress_mod", PLUGIN_PATH)
durable_dispatch = importlib.util.module_from_spec(_dd_spec)
_dd_spec.loader.exec_module(durable_dispatch)

_sa_spec = importlib.util.spec_from_file_location("superagent_stress_mod", SUPERAGENT_PATH)
superagent = importlib.util.module_from_spec(_sa_spec)
sys.modules["superagent_stress_mod"] = superagent
_sa_spec.loader.exec_module(superagent)


def _feishu_route(chat="oc_test_dm_001", route="agent:main:feishu:dm:oc_test_dm_001"):
    return {"route": route, "channel": "feishu", "chat": chat}


def _make_services(tmp_path: Path, *, cross_session_status=False, task_cards=False,
                   scheduled_routes=None, route_provider=None, roles=("dev", "ops", "test")):
    queue_base = tmp_path / "state"
    sources_root = tmp_path / "sources"
    sources_root.mkdir(parents=True, exist_ok=True)
    colleagues = {}
    for role in roles:
        qdir = queue_base / "colleague-queues" / role
        qdir.mkdir(parents=True, exist_ok=True)
        colleagues[role] = {
            "display_name": f"Colleague-{role}",
            "display_label": role.upper(),
            "queue_root": str(qdir),
            "allowed_roots": [str(sources_root)],
            "duty": f"Duty for {role}",
        }
    settings = {
        "enabled": True,
        "queue_root": str(queue_base / "analysis-queue"),
        "allowed_roots": [str(sources_root)],
        "colleagues": colleagues,
        "deliver_completions": True,
        "require_delivery_route": True,
        "direct_delivery_enabled": task_cards,
        "task_lifecycle_cards_enabled": task_cards,
        "delivery_transport_file": str(REPO / "runtime" / "hermes_completion_transport.py") if task_cards else "",
        "cross_session_status": cross_session_status,
        "scheduled_routes": scheduled_routes or {},
    }
    services = durable_dispatch.build_dispatch_services(
        settings,
        "test-profile",
        pool_type=Pool,
        delivery=lambda *_a, **_kw: None,
        route_provider=route_provider or (lambda _sid: _feishu_route()),
    )
    router = durable_dispatch.DispatchRouter(services)
    return services, router, settings


def test_parallel_3role_fanout_live_card_isolation(tmp_path: Path, monkeypatch):
    """3-role parallel fan-out + DB fallback must give each non-silent task a distinct card."""
    durable_dispatch._claimed_live_cards.clear()

    # Fake gateway modules so _claim_feishu_live_card runs the real gateway claim path,
    # deliberately returning the SAME om_live_shared card ID across concurrent calls.
    claim_calls = []

    class FakeAdapter:
        def claim_live_card(self, chat, thread_id="", session_key=""):
            claim_calls.append((chat, thread_id, session_key))
            return "om_live_shared"

    loop = asyncio.new_event_loop()
    loop_ready = threading.Event()

    def _run_loop():
        asyncio.set_event_loop(loop)
        loop_ready.set()
        loop.run_forever()

    loop_thread = threading.Thread(target=_run_loop, daemon=True)
    loop_thread.start()
    assert loop_ready.wait(timeout=5)

    fake_hermes_cli = ModuleType("hermes_cli")
    fake_pm_mod = ModuleType("hermes_cli.plugin_messages")
    fake_pm_mod._gateway_loop = lambda: (object(), loop)
    fake_gateway = ModuleType("gateway")
    fake_gw_pm = ModuleType("gateway.plugin_messages")
    fake_gw_pm.resolve = lambda *a, **kw: (SimpleNamespace(adapter=FakeAdapter()), None)

    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugin_messages", fake_pm_mod)
    monkeypatch.setitem(sys.modules, "gateway", fake_gateway)
    monkeypatch.setitem(sys.modules, "gateway.plugin_messages", fake_gw_pm)
    try:
        services, router, _ = _make_services(tmp_path, task_cards=True)

        # First ensure a silent subtask never wastes the live card claim
        silent_res = router.submit("sess-fanout", {
            "role": "dev",
            "goal": "silent subtask before fanout",
            "request_key": "fanout-silent-pre",
            "silent": True,
        })
        assert silent_res["ok"] is True and silent_res.get("silent") is True
        assert claim_calls == []

        # 3 concurrent fan-out submits across dev, ops, test
        barrier = threading.Barrier(3)

        def _submit_role(role: str):
            barrier.wait(timeout=5)
            return router.submit("sess-fanout", {
                "role": role,
                "goal": f"parallel task for {role}",
                "request_key": f"fanout-{role}-1",
            })

        with ThreadPoolExecutor(max_workers=3) as ex:
            results = list(ex.map(_submit_role, ["dev", "ops", "test"]))
    finally:
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=5)
        loop.close()

    assert all(r["ok"] and r.get("card_requested") for r in results)
    task_ids = [r["task_id"] for r in results]
    assert len(set(task_ids)) == 3

    # Verify in-process dedup let exactly 1 non-silent origin claim om_live_shared,
    # and the silent job did not get a card_message_id.
    claimed_cards = []
    for role in ("dev", "ops", "test"):
        with services[role].pool.db() as db:
            for row in db.execute("SELECT id, origin FROM jobs").fetchall():
                orig = json.loads(row["origin"])
                if row["id"] == silent_res["task_id"]:
                    assert "card_message_id" not in orig
                elif "card_message_id" in orig:
                    claimed_cards.append(orig["card_message_id"])
    assert claimed_cards == ["om_live_shared"]

    # Now verify drain_cards creates 3 distinct message_ids in TaskCardBridge
    bridge = TaskCardBridge(
        tmp_path / "cards",
        profile="test-profile",
        sender=lambda *, address, message_id, card_payload: {"ok": True, "message_id": message_id},
    )
    created_seq = []

    def _create_card(*, address, card_payload, delivery_key):
        created_seq.append(card_payload)
        return {"ok": True, "message_id": f"om_created_{len(created_seq)}"}

    transport = SimpleNamespace(
        card_bridge=bridge,
        card_creator=_create_card,
        reconcile_card=lambda _tid: {"terminal": False},
    )
    outcomes = []
    for role in ("dev", "ops", "test"):
        outcomes.extend(services[role].drain_cards(transport))
    assert len(outcomes) == 3
    assert all(o["state"] == "sent" for o in outcomes)

    cards = [bridge.get_card(tid) for tid in task_ids]
    msg_ids = [c["message_id"] for c in cards]
    assert all(isinstance(m, str) and m.startswith("om_") for m in msg_ids)
    assert len(set(msg_ids)) == 3, f"expected 3 distinct cards, got {msg_ids}"

    # Direct DB-layer collision test: even if bind_task is called directly with a stolen message_id
    b1 = bridge.bind_task("task_direct_1", _feishu_route(), "g1", "ops", "Ops", message_id="om_direct_dup")
    b2 = bridge.bind_task("task_direct_2", _feishu_route(), "g2", "test", "Test", message_id="om_direct_dup")
    assert b1["ok"] and b1["message_id"] == "om_direct_dup"
    assert b2["ok"] and b2["message_id"] is None
    # Same-task conflicting message_id still returns message_id_conflict
    b1_conflict = bridge.bind_task("task_direct_1", _feishu_route(), "g1", "ops", "Ops", message_id="om_other_unused")
    assert b1_conflict == {"ok": False, "error": "message_id_conflict"}


def test_cross_role_status_and_cancel_postures(tmp_path: Path):
    """Cross-role status/cancel respects cross_session_status, scheduled origins, and invalid roles."""
    # 1. Production posture: cross_session_status=True allows status/cancel without role
    _, router_open, _ = _make_services(tmp_path / "open", cross_session_status=True)
    ops_res = router_open.submit("sess-a", {"role": "ops", "goal": "check ops", "request_key": "k-ops"})
    test_res = router_open.submit("sess-a", {"role": "test", "goal": "check test", "request_key": "k-test"})
    assert ops_res["role"] == "ops" and test_res["role"] == "test"

    st = router_open.status("sess-a", {"task_id": ops_res["task_id"]})
    assert st["ok"] is True and st["role"] == "ops" and st["status"] == "queued"

    ca = router_open.cancel("sess-a", {"task_id": test_res["task_id"]})
    assert ca["ok"] is True and ca["role"] == "test" and ca["cancellation_requested"] is True

    # Unknown explicit role still fails validation cleanly even when cross_session_status=True
    with pytest.raises(ValueError, match="unknown administrator colleague role"):
        router_open.status("sess-a", {"task_id": ops_res["task_id"], "role": "nonexistent"})

    # 2. Strict posture: cross_session_status=False keeps non-scheduled named tasks explicit,
    # while allowing route-matched scheduled tasks to be inspected/cancelled without specifying role.
    r_match = _feishu_route("oc_sched_route", "agent:main:feishu:dm:oc_sched_route")
    r_other = _feishu_route("oc_other_route", "agent:main:feishu:dm:oc_other_route")
    caller_route = {"v": r_match}
    services_strict, router_strict, _ = _make_services(
        tmp_path / "strict",
        cross_session_status=False,
        scheduled_routes={"sched": r_match},
        route_provider=lambda _sid: caller_route["v"],
    )
    strict_ops = router_strict.submit("sess-s1", {"role": "ops", "goal": "strict ops", "request_key": "k-strict"})
    with pytest.raises(PermissionError, match="task unavailable in this session"):
        router_strict.status("sess-s1", {"task_id": strict_ops["task_id"]})
    assert router_strict.status("sess-s1", {"task_id": strict_ops["task_id"], "role": "ops"})["ok"] is True
    with pytest.raises(PermissionError, match="task unavailable in this session"):
        router_strict.status("sess-s2", {"task_id": strict_ops["task_id"], "role": "ops"})

    sched_res = services_strict["ops"].submit_scheduled(
        "scheduled status check", route_name="sched", idempotency_key="sched-status-1",
    )
    st_sched = router_strict.status("im-turn-on-route", {"task_id": sched_res["task_id"]})
    assert st_sched["ok"] is True and st_sched["role"] == "ops"
    caller_route["v"] = r_other
    with pytest.raises(PermissionError, match="task unavailable in this session"):
        router_strict.status("im-turn-off-route", {"task_id": sched_res["task_id"]})


def test_cron_session_scheduled_route_and_security_boundaries(tmp_path: Path, monkeypatch):
    """Unrouted HERMES_CRON_SESSION=1 resolves scheduled_routes cleanly; non-cron/IM spoofing fails closed."""
    r_def = _feishu_route("oc_cron_def", "agent:main:feishu:dm:oc_cron_def")
    r_proj = _feishu_route("oc_cron_proj", "agent:main:feishu:dm:oc_cron_proj")

    # 1. Single route auto-selected in unrouted cron session
    _, router_single, _ = _make_services(
        tmp_path / "single",
        scheduled_routes={"proj_a": r_proj},
        route_provider=lambda _sid: {},
    )
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    res1 = router_single.submit("cron-sess-1", {"role": "ops", "goal": "cron auto check", "request_key": "cron-1"})
    assert res1["ok"] is True
    with router_single.services["ops"].pool.db() as db:
        orig1 = json.loads(db.execute("SELECT origin FROM jobs WHERE id=?", (res1["task_id"],)).fetchone()["origin"])
    assert orig1["scheduled"] is True and orig1["chat"] == "oc_cron_proj"

    # 2. Multi-route uses 'default' or explicit scheduled_route; silent works without matching route
    _, router_multi, _ = _make_services(
        tmp_path / "multi",
        scheduled_routes={"default": r_def, "proj_a": r_proj},
        route_provider=lambda _sid: {},
    )
    res_def = router_multi.submit("cron-sess-2", {"role": "ops", "goal": "default cron", "request_key": "cron-def"})
    res_proj = router_multi.submit("cron-sess-2", {
        "role": "ops", "goal": "proj cron", "request_key": "cron-proj", "scheduled_route": "proj_a",
    })
    with router_multi.services["ops"].pool.db() as db:
        o_def = json.loads(db.execute("SELECT origin FROM jobs WHERE id=?", (res_def["task_id"],)).fetchone()["origin"])
        o_proj = json.loads(db.execute("SELECT origin FROM jobs WHERE id=?", (res_proj["task_id"],)).fetchone()["origin"])
    assert o_def["chat"] == "oc_cron_def" and o_proj["chat"] == "oc_cron_proj"

    with pytest.raises(PermissionError, match="scheduled route not registered"):
        router_multi.submit("cron-sess-2", {
            "role": "ops", "goal": "bad route", "request_key": "cron-bad", "scheduled_route": "missing",
        })

    # 3. Silent cron fallback with 0 scheduled_routes succeeds locally, non-silent fails
    _, router_none, _ = _make_services(tmp_path / "none", scheduled_routes={}, route_provider=lambda _sid: {})
    res_silent = router_none.submit("cron-sess-3", {
        "role": "ops", "goal": "silent cron", "request_key": "cron-sil", "silent": True,
    })
    assert res_silent["ok"] is True and res_silent.get("silent") is True
    with pytest.raises(PermissionError, match="scheduled_route or silent=true"):
        router_none.submit("cron-sess-3", {"role": "ops", "goal": "loud unrouted", "request_key": "cron-loud"})

    # 4. Non-cron session with empty route fails closed
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    with pytest.raises(PermissionError, match="addressable submitting conversation required"):
        router_multi.submit("unrouted-normal", {"role": "ops", "goal": "no route", "request_key": "noroute"})

    # 5. Interactive IM session cannot spoof scheduled_route
    _, router_im, _ = _make_services(
        tmp_path / "im",
        scheduled_routes={"proj_a": r_proj},
        route_provider=lambda _sid: _feishu_route("oc_real_user", "agent:main:feishu:dm:oc_real_user"),
    )
    with pytest.raises(PermissionError, match="only allowed in unrouted cron sessions"):
        router_im.submit("im-user-sess", {
            "role": "ops", "goal": "spoof target", "request_key": "spoof-1", "scheduled_route": "proj_a",
        })


def test_16_thread_idempotency_race(tmp_path: Path):
    """16 concurrent threads submitting 4 distinct (role, key) pairs must deduplicate cleanly with 0 locks."""
    services, router, _ = _make_services(tmp_path)
    barrier = threading.Barrier(16)
    work = [("ops", "rk-1"), ("test", "rk-2"), ("dev", "rk-3"), ("ops", "rk-4")]

    def _run_one(idx: int):
        role, key = work[idx % len(work)]
        barrier.wait(timeout=5)
        return key, router.submit("sess-race", {"role": role, "goal": f"race {key}", "request_key": key})

    with ThreadPoolExecutor(max_workers=16) as ex:
        outputs = list(ex.map(_run_one, range(16)))

    by_key = {}
    for key, res in outputs:
        assert res["ok"] is True
        by_key.setdefault(key, set()).add(res["task_id"])
    assert all(len(ids) == 1 for ids in by_key.values())
    total_rows = 0
    for role in ("dev", "ops", "test"):
        with services[role].pool.db() as db:
            total_rows += db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert total_rows == 4


def test_live_worker_cancel_and_card_terminalization(tmp_path: Path):
    """Cancel during live running attempt via router (without role) finalizes card cleanly as cancelled."""
    services, router, _ = _make_services(tmp_path, task_cards=True, cross_session_status=True)
    ops_svc = services["ops"]
    res = router.submit("sess-cancel-live", {"role": "ops", "goal": "long ops check", "request_key": "live-cancel-1"})
    job = res["task_id"]

    patches = []
    bridge = TaskCardBridge(
        tmp_path / "cards",
        profile="test-profile",
        sender=lambda *, address, message_id, card_payload: (
            patches.append((message_id, card_payload)) or {"ok": True, "message_id": message_id}
        ),
    )
    created = {"n": 0}
    fake_ctx = SimpleNamespace(
        profile_name="test-profile",
        get_config=lambda k, default=None: {
            "direct_delivery_enabled": True,
            "engine_root": str(REPO),
            "task_lifecycle_cards_enabled": True,
            "completion_card_enabled": False,
            "cards_root": str(tmp_path / "cards"),
        }.get(k, default),
    )
    transport = create_transport(fake_ctx, ops_svc)
    transport.card_bridge = bridge
    transport.card_creator = lambda *, address, card_payload, delivery_key: (
        created.__setitem__("n", created["n"] + 1)
        or {"ok": True, "message_id": f"om_live_c_{created['n']}"}
    )
    drain_out = ops_svc.drain_cards(transport)
    assert len(drain_out) == 1 and drain_out[0]["state"] == "sent"

    claimed_ev = threading.Event()
    worker_done = threading.Event()

    def _worker():
        claimed = ops_svc.pool.claim(lease=30)
        assert claimed is not None
        row, fd = claimed
        try:
            assert ops_svc.pool.begin_execution(row["id"], row["attempt"], mode="synchronous")
            claimed_ev.set()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if ops_svc.pool.status(row["id"])["cancel"]:
                    break
                time.sleep(0.02)
            assert ops_svc.pool.finish(row["id"], row["attempt"], False, {"reason": "user requested cancel"})
        finally:
            os.close(fd)
            worker_done.set()

    import os
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    assert claimed_ev.wait(timeout=5.0)

    cancel_res = router.cancel("sess-cancel-live", {"task_id": job})
    assert cancel_res["ok"] is True and cancel_res["role"] == "ops"
    assert worker_done.wait(timeout=5.0)

    st = router.status("sess-cancel-live", {"task_id": job})
    assert st["status"] == "cancelled"

    rec = transport.reconcile_card(job)
    assert rec["terminal"] is True
    card = bridge.get_card(job)
    assert card["finalized"] == 1 and card["status"] == "cancelled"


def test_worker_lease_expiry_reclaim_and_max_tries_exhaustion(tmp_path: Path):
    """Expired worker lease reclaims to attempt 2 cleanly; exhausting max_tries fails job terminally."""
    services, router, _ = _make_services(tmp_path)
    ops_pool = services["ops"].pool
    res = router.submit("sess-lease", {"role": "ops", "goal": "retry test", "request_key": "lease-retry-1"})
    job = res["task_id"]

    import os
    claim1 = ops_pool.claim(lease=5)
    assert claim1 is not None
    row1, fd1 = claim1
    assert row1["tries"] == 1
    os.close(fd1)  # simulate worker process vanishing before begin_execution

    # Backdate heartbeat beyond lease=5s (while staying inside START_WINDOW_S)
    with ops_pool.db() as db:
        db.execute("UPDATE jobs SET heartbeat=heartbeat-15 WHERE id=?", (job,))

    claim2 = ops_pool.claim(lease=5)
    assert claim2 is not None
    row2, fd2 = claim2
    try:
        assert row2["tries"] == 2
        assert ops_pool.begin_execution(job, row2["attempt"], mode="synchronous")
        assert ops_pool.finish(job, row2["attempt"], False, {"error": "second crash"})
    finally:
        os.close(fd2)

    st = ops_pool.status(job)
    assert st["status"] == "failed" and st["tries"] == 2
    assert ops_pool.claim(lease=30) is None


def test_lan_cluster_probe_bypasses_dead_http_proxy_and_installer_preserves_disabled_external_plugin(
    tmp_path: Path, monkeypatch
):
    """lan_cluster.probe_node must ignore HTTP(S)_PROXY; _enable_plugins must preserve disabled external plugins."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    srv_thread = threading.Thread(target=srv.serve_forever, daemon=True)
    srv_thread.start()
    try:
        for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.setenv(var, "http://127.0.0.1:1")
        nodes = {
            "local-test": {
                "display_name": "Local Test",
                "role": "host",
                "capabilities": ["http_health"],
                "http_health_url": f"http://127.0.0.1:{port}/api/health",
            }
        }
        probe = probe_node(nodes, "local-test")
        assert probe["ok"] is True
        assert probe["checks"]["http_health"]["ok"] is True
        assert probe["checks"]["http_health"]["status_code"] == 200
    finally:
        srv.shutdown()
        srv.server_close()

    # Check superagent._enable_plugins preserves langfuse in plugins.disabled
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    cfg = profile_home / "config.yaml"
    cfg.write_text(yaml.safe_dump({
        "plugins": {
            "enabled": [],
            "disabled": ["langfuse"],
            "entries": {},
        }
    }), encoding="utf-8")

    superagent._enable_plugins(profile_home, ["durable-dispatch", "langfuse"])
    after = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    enabled = set(after["plugins"].get("enabled") or [])
    disabled = set(after["plugins"].get("disabled") or [])
    assert "durable-dispatch" in enabled
    assert "langfuse" in disabled
    assert "langfuse" not in enabled


def test_doctor_colleague_queue_workers_observation_flags_unmanned_roles(tmp_path: Path, monkeypatch):
    """Doctor helper detects advertised roles without active worker units and counts stranded jobs."""
    fake_home = tmp_path / "home"
    unit_dir = fake_home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    base_frag = unit_dir / "superagent-worker-demo.service"
    base_frag.write_text("[Unit]\n", encoding="utf-8")

    profile_home = fake_home / ".hermes" / "profiles" / "demo"
    profile_home.mkdir(parents=True)
    root = tmp_path / "managed"
    reg_path = root / "state" / "colleague-team" / "registry.sqlite"
    reg_path.parent.mkdir(parents=True)
    with sqlite3.connect(reg_path) as conn:
        conn.execute("CREATE TABLE colleague_members (profile_id TEXT, member_id TEXT, role_id TEXT, status TEXT)")
        conn.execute("INSERT INTO colleague_members VALUES ('demo', 'ops', 'ops', 'active')")
        conn.execute("INSERT INTO colleague_members VALUES ('demo', 'backend', 'backend', 'active')")
        conn.execute("INSERT INTO colleague_members VALUES ('demo', 'frontend', 'frontend', 'inactive')")
        conn.execute("INSERT INTO colleague_members VALUES ('demo', 'demo', 'primary', 'active')")

    # Put 1 stranded queued job in backend's queue
    backend_pool = Pool(root / "state" / "colleague-queues" / "backend")
    backend_pool.submit("k-stranded", {"goal": "stranded work"}, origin={"route": "local", "profile": "demo", "session": "s1"})

    (profile_home / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {
            "entries": {
                "durable-dispatch": {
                    "settings": {
                        "roster_registry_path": str(reg_path),
                        "colleagues": {"ops": {"queue_root": str(root / "state/colleague-queues/ops")}},
                    }
                }
            }
        }
    }), encoding="utf-8")

    monkeypatch.setenv("HOME", str(fake_home))

    def fake_run(cmd, **_kw):
        unit = cmd[3]
        if unit == "superagent-worker-demo.service":
            out = f"LoadState=loaded\nActiveState=active\nFragmentPath={base_frag}\n"
        elif unit == "superagent-worker-demo-ops.service":
            out = "LoadState=loaded\nActiveState=active\n"
        else:
            out = "LoadState=not-found\nActiveState=inactive\n"
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(superagent, "_run", fake_run)
    obs = superagent._colleague_queue_workers_observation(profile_home, "demo", root)
    assert obs["state"] == "observed"
    assert obs["healthy"] is False
    assert obs["advertised_roles"] == ["backend", "ops"]
    assert obs["unmanned_roles"] == ["backend"]
    assert obs["unmanned_queued_jobs"] == 1


def test_non_gateway_hermes_cli_context_skips_backfill_threads(tmp_path: Path, monkeypatch):
    """Non-gateway hermes_cli contexts (dashboard/CLI/TUI) must not spawn backfill threads."""
    services, _, _ = _make_services(tmp_path)
    ops_svc = services["ops"]

    # Create a class whose __module__ mimics hermes_cli.plugins
    CliCtx = type(
        "FakeCliPluginContext",
        (),
        {
            "__module__": "hermes_cli.plugins",
            "_manager": SimpleNamespace(has_gateway_message_injector=False),
            "profile_name": "test-profile",
            "get_config": lambda self, k, default=None: {
                "deliver_completions": True,
                "engine_root": str(REPO),
            }.get(k, default),
            "inject_message": lambda self, **_kw: {"ok": True},
        },
    )
    cli_ctx = CliCtx()
    monkeypatch.delitem(sys.modules, "gateway.run", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    monkeypatch.setattr(sys, "argv", ["hermes", "dashboard", "--port", "9119"])

    assert durable_dispatch._is_gateway_backfill_host(cli_ctx) is False
    assert durable_dispatch.start_backfill(cli_ctx, ops_svc) is None

    # Host PluginContext with gateway message injector wired stays enabled
    cli_ctx._manager.has_gateway_message_injector = True
    assert durable_dispatch._is_gateway_backfill_host(cli_ctx) is True


def test_backfill_waits_until_transport_is_ready(tmp_path: Path, monkeypatch):
    """Backfill threads must wait while transport.is_ready() is False, then drain once True."""
    from agentcore import completion_backfill

    services, router, _ = _make_services(tmp_path, task_cards=True)
    ops_svc = services["ops"]
    ops_svc.delivery = completion_backfill
    res = router.submit("sess-ready", {"role": "ops", "goal": "ready probe test", "request_key": "ready-1"})
    job = res["task_id"]

    import os
    claimed = ops_svc.pool.claim(lease=30)
    assert claimed is not None
    row, fd = claimed
    try:
        assert ops_svc.pool.begin_execution(job, row["attempt"], mode="synchronous")
        assert ops_svc.pool.finish(job, row["attempt"], True, {"summary": "done"})
    finally:
        os.close(fd)

    ready_flag = {"ready": False}
    deliver_calls = []
    create_calls = []

    class _FakeTransport:
        card_bridge = TaskCardBridge(
            tmp_path / "cards",
            profile="test-profile",
            sender=lambda **_kw: {"ok": True, "message_id": "om_card_ready"},
        )

        def is_ready(self):
            return ready_flag["ready"]

        def card_creator(self, *, address, card_payload, delivery_key):
            create_calls.append(delivery_key)
            return {"ok": True, "message_id": "om_card_ready"}

        def deliver(self, address, message, *, idempotency_key):
            deliver_calls.append(idempotency_key)
            return {"accepted": True, "receipt_id": f"rcpt-{idempotency_key[:8]}"}

    transport_mod = ModuleType("fake_transport_mod")
    transport_mod.create_transport = lambda ctx, svc: _FakeTransport()
    transport_file = tmp_path / "fake_transport.py"
    transport_file.write_text("# stub overridden via importlib\n", encoding="utf-8")

    monkeypatch.setattr(
        durable_dispatch.importlib.util,
        "spec_from_file_location",
        lambda name, path: SimpleNamespace(loader=SimpleNamespace(exec_module=lambda m: setattr(m, "create_transport", transport_mod.create_transport))),
    )
    monkeypatch.setattr(
        durable_dispatch.importlib.util,
        "module_from_spec",
        lambda spec: ModuleType("_durable_completion_transport"),
    )

    unload_callbacks = []
    ctx = SimpleNamespace(
        profile_name="test-profile",
        get_config=lambda k, default=None: {
            "deliver_completions": True,
            "delivery_interval": 0.05,
            "delivery_transport_file": str(transport_file),
            "report_delivery_health": False,
        }.get(k, default),
        on_unload=lambda cb: unload_callbacks.append(cb),
    )

    t = durable_dispatch.start_backfill(ctx, ops_svc)
    assert t is not None
    try:
        # While is_ready() is False, neither delivery nor card creation may fire.
        time.sleep(0.15)
        assert deliver_calls == []
        assert create_calls == []

        # Flip ready -> True and confirm completion + card both drain promptly.
        ready_flag["ready"] = True
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if deliver_calls and create_calls:
                break
            time.sleep(0.02)
        assert job in deliver_calls
        assert len(create_calls) == 1
    finally:
        for cb in unload_callbacks:
            cb()


def test_update_restarts_bound_hermes_dashboard_service_only(tmp_path: Path, monkeypatch):
    """superagent.update stops/restarts hermes-dashboard.service only when bound to profile_home."""
    fake_home = tmp_path / "home"
    unit_dir = fake_home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    profile_home = fake_home / ".hermes" / "profiles" / "demo"
    other_profile_home = fake_home / ".hermes" / "profiles" / "demo2"
    profile_home.mkdir(parents=True)
    other_profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text("plugins:\n  enabled: []\n", encoding="utf-8")

    # Helper checks: prefix mismatch (demo2) must not match demo; per-profile wins over shared.
    shared_unit = unit_dir / "hermes-dashboard.service"
    shared_unit.write_text(f'[Service]\nEnvironment="HERMES_HOME={other_profile_home}"\n', encoding="utf-8")
    assert superagent._discover_profile_dashboard_service(unit_dir, "demo", profile_home) is None

    shared_unit.write_text(f'[Service]\nEnvironment="HERMES_HOME={profile_home}"\nWorkingDirectory={profile_home}\n', encoding="utf-8")
    assert superagent._discover_profile_dashboard_service(unit_dir, "demo", profile_home) == "hermes-dashboard.service"

    per_profile_unit = unit_dir / "hermes-dashboard-demo.service"
    per_profile_unit.write_text("[Service]\n", encoding="utf-8")
    assert superagent._discover_profile_dashboard_service(unit_dir, "demo", profile_home) == "hermes-dashboard-demo.service"
    per_profile_unit.unlink()

    # Exercise superagent.update with matching vs non-matching shared dashboard unit
    monkeypatch.setattr(superagent.Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setattr(superagent, "_check_deploy_source_guard", lambda args, plan, root: ([], []))
    monkeypatch.setattr(superagent, "_update_config_snapshot", lambda args, ph, root: ({}, "hermes-gateway-demo.service"))
    monkeypatch.setattr(superagent, "_plugin_link_snapshot", lambda ph: {})
    monkeypatch.setattr(superagent, "_declared_runtime_services", lambda args: [])
    monkeypatch.setattr(superagent, "_declared_runtime_scripts", lambda args, svcs: {})
    monkeypatch.setattr(superagent, "_assert_service_quiescent", lambda name: None)
    monkeypatch.setattr(superagent, "_wait_for_running_jobs_drain", lambda root, timeout_s=30.0: None)
    monkeypatch.setattr(superagent, "install", lambda args, plan: 0)
    monkeypatch.setattr(superagent, "_release_valid", lambda path: True)
    monkeypatch.setattr(superagent, "_prune_old_releases", lambda root, rel, mpath, keep=3: None)

    sa_base = fake_home / "superagent"
    root = sa_base / "demo"
    release_dir = root / "releases" / "r1"
    release_dir.mkdir(parents=True)
    manifest_file = root / "install-manifest.json"
    monkeypatch.setattr(superagent, "_manifest", lambda args: (manifest_file, {"release": str(release_dir)}))

    for expected_restart, target_home in ((True, profile_home), (False, other_profile_home)):
        shared_unit.write_text(f'[Service]\nEnvironment="HERMES_HOME={target_home}"\n', encoding="utf-8")
        systemctl_ops = []

        def fake_run(cmd, **_kw):
            systemctl_ops.append(list(cmd))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(superagent, "_run", fake_run)
        args = SimpleNamespace(
            profile="demo",
            profile_home=str(profile_home),
            root=sa_base,
            runtime_manifest=None,
            drain_timeout=1.0,
            keep=3,
        )
        plan = {"profile_home": str(profile_home)}
        rc = superagent.update(args, plan)
        assert rc == 0
        stops = [c[3] for c in systemctl_ops if len(c) >= 4 and c[2] == "stop"]
        starts = [c[3] for c in systemctl_ops if len(c) >= 4 and c[2] == "start"]
        assert ("hermes-dashboard.service" in stops) is expected_restart
        assert ("hermes-dashboard.service" in starts) is expected_restart


