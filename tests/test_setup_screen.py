"""Unit tests for colleague screen contracts, WorkerSpec screen extensions, and setup_screen CLI."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from agentcore.colleague_catalog import default_dev_catalog
from agentcore.colleague_dev import (
    DEFAULT_SCREEN_POLICY,
    DEV_ROLES,
    DEV_ROLE_SCREEN,
    get_dev_roles,
    get_role_screen_policy,
    register_role_override,
    reset_role_overrides,
    role_supports_screen,
)
from agentcore.profile_spec import (
    BusinessProfileSpec,
    WorkerSpec,
    load_profile_spec,
)
from install import setup_profile, setup_screen


class TestColleagueScreenContract:
    def teardown_method(self) -> None:
        reset_role_overrides()

    def test_canonical_roles_screen_flags_and_policies(self) -> None:
        expected_enabled = {"architect", "test", "frontend", "ops"}
        expected_disabled = {"cases", "reviewer", "triage", "backend"}
        assert expected_enabled | expected_disabled == set(DEV_ROLES.keys())
        assert set(DEV_ROLE_SCREEN.keys()) == set(DEV_ROLES.keys())

        # Strict catalog 4-field parity preserved for colleague_catalog
        assert get_dev_roles() == DEV_ROLES
        assert len(default_dev_catalog()["colleagues"]) == 8

        with_screen = get_dev_roles(include_screen=True)
        for role in expected_enabled:
            assert DEV_ROLE_SCREEN[role]["has_screen"] is True
            assert with_screen[role]["has_screen"] is True
            assert role_supports_screen(role) is True
            policy = get_role_screen_policy(role)
            assert policy == DEFAULT_SCREEN_POLICY
            for key in ("display_name", "duty", "can_do", "boundaries"):
                assert key in DEV_ROLES[role]

        for role in expected_disabled:
            assert DEV_ROLE_SCREEN[role]["has_screen"] is False
            assert with_screen[role]["has_screen"] is False
            assert role_supports_screen(role) is False
            policy = get_role_screen_policy(role)
            assert policy == DEFAULT_SCREEN_POLICY

        assert role_supports_screen("non_existent_custom_role") is False
        with pytest.raises(ValueError, match="unknown_role"):
            get_role_screen_policy("non_existent_custom_role")

    def test_register_role_override_updates_screen_capabilities(self) -> None:
        assert role_supports_screen("backend") is False
        register_role_override(
            "backend",
            {
                "has_screen": True,
                "screen_policy": {"geometry": "1920x1080", "placement": "gateway"},
            },
        )
        assert role_supports_screen("backend") is True
        policy = get_role_screen_policy("backend")
        assert policy["geometry"] == "1920x1080"
        assert policy["placement"] == "gateway"
        assert policy["auto_start"] is False
        assert policy["idle_stop_minutes"] == 30

        merged_roles = get_dev_roles()
        assert merged_roles["backend"]["screen_policy"]["idle_stop_minutes"] == 30

        reset_role_overrides()
        assert role_supports_screen("backend") is False
        assert get_role_screen_policy("backend")["geometry"] == "1440x900"


class TestWorkerSpecScreenExtensions:
    def test_worker_spec_defaults_and_validation(self) -> None:
        default_worker = WorkerSpec(role="ops")
        assert default_worker.enable_screen is False
        assert default_worker.screen_geometry == "1440x900"
        assert default_worker.screen_auto_start is False
        assert default_worker.screen_idle_stop_minutes == 30
        assert default_worker.screen_min_free_memory_mb == 1536
        assert default_worker.screen_placement == "auto"
        assert default_worker.validate() == []

        valid_worker = WorkerSpec(
            role="frontend",
            enable_screen=True,
            screen_geometry="1920x1080",
            screen_placement="gateway",
            screen_idle_stop_minutes=0,
            screen_min_free_memory_mb=0,
        )
        assert valid_worker.validate() == []

        bad_geom = WorkerSpec(role="frontend", screen_geometry="14x90")
        assert any("Invalid screen_geometry" in e for e in bad_geom.validate())

        bad_geom_alpha = WorkerSpec(role="frontend", screen_geometry="wide")
        assert any("Invalid screen_geometry" in e for e in bad_geom_alpha.validate())

        bad_placement = WorkerSpec(role="frontend", screen_placement="floating")
        assert any("Invalid screen_placement" in e for e in bad_placement.validate())

        bad_negatives = WorkerSpec(
            role="frontend",
            screen_idle_stop_minutes=-1,
            screen_min_free_memory_mb=-512,
        )
        neg_errors = bad_negatives.validate()
        assert any("screen_idle_stop_minutes" in e for e in neg_errors)
        assert any("screen_min_free_memory_mb" in e for e in neg_errors)

    def test_render_bot_desktop_config_exact_upstream_keys(self) -> None:
        disabled = WorkerSpec(role="cases", enable_screen=False)
        cfg_off = disabled.render_bot_desktop_config()
        assert set(cfg_off.keys()) == {"platform_toolsets", "bot_desktop", "computer_use"}
        assert cfg_off["platform_toolsets"] == []
        assert set(cfg_off["bot_desktop"].keys()) == {
            "geometry",
            "auto_start",
            "min_free_memory_mb",
            "idle_stop_minutes",
            "placement",
        }
        assert "enabled" not in cfg_off["bot_desktop"]
        assert "resolution" not in cfg_off["bot_desktop"]
        assert cfg_off["computer_use"] == {
            "cua_telemetry": False,
            "autostart": False,
            "max_image_dimension": 1456,
            "capture_after_mode": "som",
        }

        enabled = WorkerSpec(
            role="frontend",
            enable_screen=True,
            screen_geometry="1600x900",
            screen_auto_start=True,
            screen_idle_stop_minutes=45,
            screen_min_free_memory_mb=2048,
            screen_placement="gateway",
        )
        cfg_on = enabled.render_bot_desktop_config()
        assert cfg_on["platform_toolsets"] == ["computer_use"]
        assert cfg_on["bot_desktop"] == {
            "geometry": "1600x900",
            "auto_start": True,
            "min_free_memory_mb": 2048,
            "idle_stop_minutes": 45,
            "placement": "gateway",
        }
        assert cfg_on["computer_use"]["autostart"] is True

    def test_profile_overlay_service_env_and_round_trip(self, tmp_path: Path, capsys) -> None:
        spec = BusinessProfileSpec(
            profile_id="visual_qa",
            description="Visual verification profile",
            workers=[
                WorkerSpec(
                    role="frontend",
                    enable_screen=True,
                    screen_geometry="1920x1080",
                    screen_auto_start=True,
                    screen_idle_stop_minutes=20,
                    screen_min_free_memory_mb=2048,
                    screen_placement="gateway",
                ),
                WorkerSpec(role="backend", enable_screen=False),
            ],
        )

        overlay = spec.render_hermes_screen_overlay()
        assert overlay["platform_toolsets"] == ["computer_use"]
        assert overlay["bot_desktop"]["geometry"] == "1920x1080"
        assert overlay["bot_desktop"]["placement"] == "gateway"
        assert overlay["bot_desktop"]["auto_start"] is True

        fe_unit = spec.render_worker_service(spec.workers[0])
        assert 'Environment="SUPERAGENT_ENABLE_SCREEN=1"' in fe_unit
        assert 'Environment="SUPERAGENT_SCREEN_GEOMETRY=1920x1080"' in fe_unit

        be_unit = spec.render_worker_service(spec.workers[1])
        assert "SUPERAGENT_ENABLE_SCREEN" not in be_unit
        assert "SUPERAGENT_SCREEN_GEOMETRY" not in be_unit

        raw = spec.to_dict()
        reloaded = load_profile_spec(raw)
        assert reloaded.workers[0].enable_screen is True
        assert reloaded.workers[0].screen_geometry == "1920x1080"
        assert reloaded.workers[0].screen_auto_start is True
        assert reloaded.workers[0].screen_idle_stop_minutes == 20
        assert reloaded.workers[0].screen_min_free_memory_mb == 2048
        assert reloaded.workers[0].screen_placement == "gateway"
        assert reloaded.workers[1].enable_screen is False

        spec_file = tmp_path / "profile.json"
        spec_file.write_text(json.dumps(raw), encoding="utf-8")
        rc = setup_profile.cmd_inspect(spec_file)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Screen : enabled (1920x1080, placement=gateway, auto_start=True)" in out


class TestSetupScreenCLI:
    def test_doctor_pass_and_fail(self, tmp_path: Path, monkeypatch, capsys) -> None:
        hermes_home = tmp_path / "hermes"
        lease_dir = hermes_home / "bot-desktop"
        lease_dir.mkdir(parents=True)
        (lease_dir / "lease.json").write_text(
            json.dumps(
                {
                    "holder": "human",
                    "viewer_id": "secret-capability-token-xyz",
                    "since": 1700000000.0,
                    "reason": "mfa_login",
                    "epoch": 2,
                }
            ),
            encoding="utf-8",
        )
        meminfo = tmp_path / "meminfo"
        meminfo.write_text("MemTotal:       8388608 kB\nMemAvailable:   4194304 kB\n", encoding="utf-8")
        os_rel = tmp_path / "os-release"
        os_rel.write_text('ID=ubuntu\nID_LIKE=debian\n', encoding="utf-8")

        # All binaries present
        monkeypatch.setattr(setup_screen.shutil, "which", lambda b: f"/usr/bin/{b}")
        rc_ok = setup_screen.cmd_doctor(
            hermes_home,
            min_free_mb=1536,
            as_json=True,
            meminfo_path=meminfo,
            os_release_path=os_rel,
        )
        assert rc_ok == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["missing_binaries"] == []
        assert payload["memory"]["available_mb"] == 4096
        assert payload["lease"]["holder"] == "human"
        assert payload["lease"]["viewer_hash"] is not None
        assert "secret-capability-token-xyz" not in json.dumps(payload)

        # Missing Xvnc -> returns 1 and recommends install command
        monkeypatch.setattr(
            setup_screen.shutil,
            "which",
            lambda b: None if b == "Xvnc" else f"/usr/bin/{b}",
        )
        rc_fail = setup_screen.cmd_doctor(
            hermes_home,
            min_free_mb=1536,
            as_json=False,
            meminfo_path=meminfo,
            os_release_path=os_rel,
        )
        assert rc_fail == 1
        out_fail = capsys.readouterr().out
        assert "[MISSING] Xvnc" in out_fail
        assert "tigervnc-standalone-server" in out_fail

    def test_install_deps_dry_run_excludes_full_xfce4_metapackage(
        self, tmp_path: Path, capsys
    ) -> None:
        os_rel = tmp_path / "os-release"
        os_rel.write_text('ID=debian\n', encoding="utf-8")

        rc = setup_screen.cmd_install_deps(distro_arg="auto", apply=False, os_release_path=os_rel)
        assert rc == 0
        out = capsys.readouterr().out
        assert "[DRY-RUN]" in out
        assert "tigervnc-standalone-server" in out
        assert "xfce4-panel" in out
        assert "xfwm4" in out
        assert "xfdesktop4" in out
        # Must NOT install the full 'xfce4' metapackage token
        debian_cmd = setup_screen.build_install_deps_command("debian")
        assert "xfce4" not in debian_cmd
        assert "xfce4" not in setup_screen.build_install_deps_command("fedora")
        assert "xfce4" not in setup_screen.build_install_deps_command("arch")

    def test_configure_dry_run_and_apply_preserves_existing_settings(
        self, tmp_path: Path, capsys
    ) -> None:
        hermes_home = tmp_path / "hermes_profile"
        hermes_home.mkdir()
        cfg_file = hermes_home / "config.yaml"
        cfg_file.write_text(
            "model: gpt-test\nplatform_toolsets:\n  cli:\n    - task_status\n",
            encoding="utf-8",
        )

        # 1. Dry-run does not mutate config.yaml
        rc_dry = setup_screen.main(
            [
                "configure",
                "--hermes-home",
                str(hermes_home),
                "--geometry",
                "1600x900",
                "--placement",
                "gateway",
            ]
        )
        assert rc_dry == 0
        out_dry = capsys.readouterr().out
        assert "[DRY-RUN]" in out_dry
        assert "bot_desktop" in out_dry
        assert "bot_desktop" not in cfg_file.read_text(encoding="utf-8")

        # 2. Apply merges settings cleanly
        rc_apply = setup_screen.main(
            [
                "configure",
                "--hermes-home",
                str(hermes_home),
                "--geometry",
                "1600x900",
                "--auto-start",
                "--placement",
                "gateway",
                "--idle-stop-minutes",
                "45",
                "--min-free-mb",
                "2048",
                "--apply",
            ]
        )
        assert rc_apply == 0
        capsys.readouterr()

        loaded = setup_screen._load_yaml_or_json(cfg_file.read_text(encoding="utf-8"))
        assert loaded["model"] == "gpt-test"
        assert loaded["platform_toolsets"] == {"cli": ["task_status", "computer_use"]}
        assert loaded["bot_desktop"] == {
            "geometry": "1600x900",
            "auto_start": True,
            "min_free_memory_mb": 2048,
            "idle_stop_minutes": 45,
            "placement": "gateway",
        }
        assert loaded["computer_use"] == {
            "cua_telemetry": False,
            "autostart": True,
            "max_image_dimension": 1456,
            "capture_after_mode": "som",
        }

        # Omitted platform_toolsets must remain omitted so Hermes _HERMES_CORE_TOOLS stays intact
        omitted_merged = setup_screen.merge_screen_config({"model": "gpt-test"})
        assert "platform_toolsets" not in omitted_merged

        # Explicit hermes-* bundle already contains computer_use and is not duplicated
        bundle_merged = setup_screen.merge_screen_config(
            {"platform_toolsets": {"feishu": ["hermes-feishu"]}}
        )
        assert bundle_merged["platform_toolsets"] == {"feishu": ["hermes-feishu"]}

        # Also verify list-format platform_toolsets handling
        list_merged = setup_screen.merge_screen_config(
            {"platform_toolsets": ["memory_bridge"]},
            geometry="1440x900",
        )
        assert list_merged["platform_toolsets"] == ["memory_bridge", "computer_use"]

    def test_status_delegation_with_mocked_subprocess(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        hermes_home = tmp_path / "hermes_home"
        (hermes_home / "bot-desktop").mkdir(parents=True)
        (hermes_home / "bot-desktop" / "lease.json").write_text(
            json.dumps({"holder": "agent", "viewer_id": "raw-secret-token", "epoch": 5}),
            encoding="utf-8",
        )
        fake_hermes = tmp_path / "hermes"
        fake_hermes.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_hermes.chmod(0o755)

        captured: dict = {}

        def fake_run(argv, env=None, **kwargs):
            captured["argv"] = argv
            captured["env"] = env
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(setup_screen.subprocess, "run", fake_run)

        rc = setup_screen.main(
            [
                "status",
                "--hermes-home",
                str(hermes_home),
                "--hermes-bin",
                str(fake_hermes),
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "Lease Holder   : agent" in out
        assert "raw-secret-token" not in out
        assert captured["argv"] == [str(fake_hermes), "computer-use", "screen", "status"]
        assert captured["env"]["HERMES_HOME"] == str(hermes_home.resolve())

    def test_web_portal_rfb_gate_and_lease_roundtrip(self, tmp_path: Path) -> None:
        from agentcore.screen_lease import (
            acquire_bot_desktop_lease,
            release_bot_desktop_lease,
            viewer_holds_lease,
        )
        from install.screen_web_portal import RfbInputGate, inspect_profile_screen

        hermes_home = tmp_path / "profiles" / "alpha"
        hermes_home.mkdir(parents=True)

        st_acquire = acquire_bot_desktop_lease(hermes_home, "viewer-123", reason="login")
        assert st_acquire.holder == "human"
        assert st_acquire.epoch == 1
        assert viewer_holds_lease(hermes_home, "viewer-123") is True
        assert viewer_holds_lease(hermes_home, "viewer-other") is False

        snap = inspect_profile_screen("alpha", hermes_home, viewer_id="viewer-123")
        assert snap["viewer_holds_control"] is True
        assert snap["lease"]["is_human_takeover"] is True

        # Verify "<pid> <timestamp>" format in launcher.pid is parsed accurately
        import os as _os
        bd = hermes_home / "bot-desktop"
        (bd / "launcher.pid").write_text(f"{_os.getpid()} 1794386873.63\n", encoding="utf-8")
        (bd / "display").write_text("20\n", encoding="utf-8")
        (bd / "rfb.sock").write_text("", encoding="utf-8")
        snap_live = inspect_profile_screen("alpha", hermes_home, viewer_id="viewer-123")
        assert snap_live["running"] is True
        assert snap_live["pid"] == _os.getpid()
        assert snap_live["display"] == ":20"

        # Non-holder cannot release without force
        st_ignored = release_bot_desktop_lease(hermes_home, "viewer-other", force=False)
        assert st_ignored.holder == "human"

        # Holder releases cleanly
        st_rel = release_bot_desktop_lease(hermes_home, "viewer-123", force=False)
        assert st_rel.holder == "agent"
        assert st_rel.epoch == 2

        # RFB byte gate forces shared=1 and drops KeyEvent(4)/PointerEvent(5) when not holding lease
        allowed = False
        gate = RfbInputGate(lambda: allowed)
        handshake = b"RFB 003.008\n\x01\x00"  # client sent shared=0
        out_hs = gate.feed(handshake)
        assert out_hs == b"RFB 003.008\n\x01\x01"  # forced shared=1

        fb_req = bytes([3, 1, 0, 0, 0, 0, 5, 160, 3, 132])  # FramebufferUpdateRequest (10B)
        key_ev = bytes([4, 1, 0, 0, 0, 0, 0, 65])           # KeyEvent (8B)
        ptr_ev = bytes([5, 1, 0, 10, 0, 20])                # PointerEvent (6B)
        assert gate.feed(fb_req + key_ev + ptr_ev) == fb_req

        allowed = True
        assert gate.feed(key_ev + ptr_ev) == key_ev + ptr_ev
