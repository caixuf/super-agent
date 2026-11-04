"""Reproduce HFC admission completion authority validation on Hermes v0.22.5.

Runs under the real Hermes runtime with real SessionAuthority, real SessionDB,
and real admission_completion.prepare(), diagnosing why authority validation fails.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Ensure hermes checkout is importable
HERMES_ROOT = Path("/home/caixuf/.hermes/superagent/caixuf/hermes/v0.22.5-bf0203a794a0-closeout-20261103")
PM_VENV = Path("/home/caixuf/.hermes/installs/abb9058e13a560a5/environments/71ae9d9b18854c699478f020c4f4c6de/venv/lib/python3.14/site-packages")

if not HERMES_ROOT.is_dir() or not PM_VENV.is_dir():
    pytest.skip("Hermes v0.22.5 closeout candidate and HFC PM venv required for this test", allow_module_level=True)

sys.path.insert(0, str(HERMES_ROOT))
if PM_VENV.is_dir() and str(PM_VENV) not in sys.path:
    sys.path.insert(0, str(PM_VENV))


def test_reproduce_real_authority_validation(tmp_path, monkeypatch):
    # This legacy HFC integration is optional and requires its installed
    # package, not merely a directory that happens to be a Python venv.
    pytest.importorskip("hermes_feishu_card.admission_completion", reason="installed HFC package unavailable")
    pytest.importorskip("hermes_feishu_card.hook_runtime", reason="installed HFC package unavailable")
    async def _test():
        home_path = tmp_path / ".hermes" / "profiles" / "caixuf"
        home_path.mkdir(parents=True, exist_ok=True)
        hfc_state = home_path / "hfc_state"
        hfc_state.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HERMES_HOME", str(home_path))
        monkeypatch.setenv("HERMES_FEISHU_CARD_V2", "1")
        monkeypatch.setenv("HERMES_FEISHU_CARD_STATE_DIR", str(hfc_state))

        from gateway.config import GatewayConfig, Platform, PlatformConfig
        from gateway.session import SessionStore, SessionSource, build_session_key
        from gateway.session_authority import initialize_session_authority
        from gateway.platforms.event import MessageEvent
        from plugins.platforms.feishu.adapter import FeishuAdapter
        import hermes_feishu_card.admission_completion as ac
        import hermes_feishu_card.hook_runtime as hr

        # 1. Build real store, db and runner
        store = SessionStore(home_path / "sessions", GatewayConfig())
        db = store._db
        session_id = "test_sess_001"
        chat_id = "oc_98cb0f77db889888f0f243722625340f"
        user_id = "ou_07ecd7729997d428f5588170c1863491"
        db.create_session(session_id, source="feishu", model="flash", cwd=str(home_path))

        adapter = object.__new__(FeishuAdapter)
        adapter._app_id = "cli_a727c6b755b9500c"
        adapter._bot_open_id = "ou_bot"
        adapter._bot_user_id = ""
        adapter._bot_name = "superagent"
        adapter._allow_all_dm = True
        adapter._admins = {user_id}
        adapter._allow_bots = "none"
        adapter._group_policy = "allowlist"
        adapter._default_group_policy = "allowlist"
        adapter._allowed_group_users = frozenset()
        adapter.config = SimpleNamespace(card_replies=True)

        from gateway.run import GatewayRunner
        runner = GatewayRunner(GatewayConfig())
        runner.session_store = store
        runner._session_db = db
        runner.adapters = {Platform.FEISHU: adapter}
        runner._is_user_authorized_for_source = lambda *a, **k: True

        authority = await initialize_session_authority(runner, profile_id=str(home_path), instance_id="gateway")
        runner.session_authority = authority

        source = SessionSource(
            platform=Platform.FEISHU,
            chat_type="dm",
            chat_id=chat_id,
            user_id=user_id,
        )
        session_key = build_session_key(source)
        msg_id = "om_x100b61a32e66e0b0de67d2b08f2fcf2"

        event = MessageEvent(
            text="sleep 30",
            source=source,
            message_id=msg_id,
        )
        event.internal = False

        # 2. Admit native message into authority
        receipt = await authority.admit_native(event)
        admission_id = receipt.admission_id

        # 3. Simulate drain claim and execution
        ref = receipt.ref
        session_id = ref.session_id
        live = authority.sessions[ref.session_id]

        from hermes_state_runtime import get_session_admission, claim_session_input
        # Claim input so generation is allocated and status becomes 'started'!
        claimed_row = claim_session_input(authority.db, epoch=authority.epoch, session_id=ref.session_id, head=admission_id)
        assert claimed_row is not None
        row = claimed_row
        authority._stamp_claim(ref, live, row)

        import gateway.session_ingress as si

        print("ac file:", ac.__file__)
        captured_result = {}
        reasons = []
        monkeypatch.setattr(ac.completion_diagnostics, "prepare_failed", lambda r: reasons.append(r))

        async def fake_handle_message(ev):
            runner._session_state(session_key).persistent.run_generation = row["generation"]
            turn_locals = {
                "self": runner,
                "event": ev,
                "source": source,
                "session_key": session_key,
                "run_generation": row["generation"],
                "session_entry": SimpleNamespace(session_id=session_id),
                "response": "验收完成",
                "agent_result": {},
                "message_id": msg_id,
                "answer": "验收完成",
                "duration": 1.5,
                "model": "flash",
                "tokens": {"input_tokens": 10, "output_tokens": 10},
                "context": {"used_tokens": 20, "max_tokens": 1000},
            }
            print("\n=== STEP BY STEP DIAGNOSIS OF first_card_guard ===")
            verified = ac._verified()
            print("1. verified:", verified)
            claim = ac._claim_frame(verified[0]) if verified else None
            print("2. claim:", claim)
            auth = claim["authority"] if claim else None
            c_ref = claim["ref"] if claim else None
            c_row = claim["row"] if claim else None
            c_ev = claim.get("event") if claim else None
            print("3. authority:", auth, "runner is auth.runner:", (turn_locals.get("self") is getattr(auth, "runner", None)))
            print("4. claim event is ev:", (c_ev is ev))
            route = ac._route(ev, "default")
            print("5. route:", route)
            route_src = ac._route_source(source, route[-1], "default") if route else None
            print("6. route_source matches:", (route_src == route))
            internal = getattr(ev, "internal", False)
            print("7. internal:", internal, "claimed_owner:", getattr(ev, "_claimed_owner", None))
            owner = getattr(ev, "_claimed_owner", None)
            print("8. owner check:", (type(owner) is tuple and len(owner) == 2 and getattr(c_ref, "session_id", None) == owner[0] and c_row.get("generation") == owner[1]))
            print("9. _authority_profile_matches:", ac._authority_profile_matches(auth, "default"))
            adapter = runner._adapter_for_source(source)
            print("10. adapter:", adapter, "app_id:", getattr(adapter, "_app_id", None))
            
            start_locals = {
                "self": runner,
                "event": ev,
                "source": source,
                "_quick_key": session_key,
                "run_generation": row["generation"],
                "message_id": msg_id,
            }
            profile, p_source = hr._profile_identity(turn_locals, source, None)
            print("Extracted profile identity:", profile, p_source)
            print("=== TRACING first_card_guard(start_locals, profile) ===")
            route_p = ac._route(ev, profile)
            print("A. route(profile):", route_p)
            route_src_p = ac._route_source(source, route_p[-1], profile) if route_p else None
            print("B. route_source(profile):", route_src_p)
            print("C. _authority_profile_matches(auth, profile):", ac._authority_profile_matches(auth, profile))
            
            guard_p = ac.first_card_guard(start_locals, profile)
            print("guard_p:", guard_p)

            mode = hr.admission_completion_mode(turn_locals)
            print("\n--- In-call HFC admission mode:", mode)
            can_stage = hr.can_stage_exact_base_completion(turn_locals)
            print("--- Can stage:", can_stage)
            # Monitor stage diagnostics
            stage_reasons = []
            monkeypatch.setattr(hr, "_warn_stage_diagnostic", lambda r: stage_reasons.append(r))
            monkeypatch.setattr(hr, "_fetch_delivery_policy_sync", lambda *a, **k: {"ok": True, "disposition": "card", "ttl_ms": 500})
            staged = await hr.stage_message_completed_from_hermes_locals_async(turn_locals)
            print("--- Staged result:", staged)
            print("--- Stage diagnostic reasons:", stage_reasons)
            print("--- Captured prepare failed reasons:", reasons)
            captured_result["reasons"] = list(reasons)
            captured_result["mode"] = mode
            captured_result["can_stage"] = can_stage
            captured_result["staged"] = staged
            return "验收完成"

        runner._handle_message = fake_handle_message

        # Execute under execute_admission so stack frame and ContextVar are authentic!
        # Also simulate the drain task setting live.task
        async def run_in_drain():
            with live.event_stream.lock:
                authority.executing[ref.session_id] = row["generation"]
                live.event_stream.execution = {
                    "authority_epoch": authority.epoch,
                    "execution_generation": row["generation"],
                    "admission_id": admission_id,
                }
            return await si.execute_admission(authority, ref, row)

        drain_task = asyncio.create_task(run_in_drain())
        live.task = drain_task
        await drain_task

    asyncio.run(_test())
