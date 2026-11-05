"""Tests for generic business profile specification and scaffolding."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

from agentcore.profile_spec import (
    BusinessProfileSpec,
    GatewaySpec,
    MemorySpec,
    ProactiveHooksSpec,
    WorkerSpec,
    load_profile_file,
    load_profile_spec,
    package_profile_bundle,
    quote_systemd,
)


class TestProfileSpec(unittest.TestCase):
    def test_quote_systemd(self) -> None:
        self.assertEqual(quote_systemd("normal_value"), '"normal_value"')
        self.assertEqual(quote_systemd('with"quotes'), '"with\\"quotes"')
        self.assertEqual(quote_systemd("with%percent"), '"with%%percent"')
        with self.assertRaises(ValueError):
            quote_systemd("with\nnewline")
        with self.assertRaises(ValueError):
            quote_systemd("with\0nul")

    def test_validation_errors(self) -> None:
        # Invalid profile id
        spec = BusinessProfileSpec(profile_id="invalid id with spaces")
        errors = spec.validate()
        self.assertTrue(any("Invalid profile_id" in e for e in errors))

        # Duplicate roles and invalid tier
        spec = BusinessProfileSpec(
            profile_id="test_profile",
            workers=[
                WorkerSpec(role="analyst", model_tier="unknown_tier"),
                WorkerSpec(role="analyst"),
            ],
        )
        errors = spec.validate()
        self.assertTrue(any("Duplicate worker role" in e for e in errors))
        self.assertTrue(any("Invalid model_tier" in e for e in errors))

    def test_worker_spec_validations(self) -> None:
        w = WorkerSpec(role="valid_worker", concurrency=0, timeout=-1, max_file=-5)
        errors = w.validate()
        self.assertEqual(len(errors), 3)

    def test_render_systemd_units(self) -> None:
        spec = BusinessProfileSpec(
            profile_id="sample_domain",
            description="Sample Domain Description",
            managed_root="/opt/superagent/sample",
            workers=[
                WorkerSpec(
                    role="runner",
                    model_tier="pro",
                    workdir="/opt/workspace",
                    duty="Main execution runner",
                    max_file=104857600,
                    env={"EXTRA_FLAG": "1"},
                )
            ],
            proactive_hooks=ProactiveHooksSpec(
                config_file="config/hooks.json",
                timer_schedule="*:00/15",
            ),
            memory=MemorySpec(
                source="memories/MEMORY.md",
                user="sample_user",
                agent="sample_agent",
            ),
        )

        units = spec.render_all_units()
        self.assertIn("superagent-worker-sample_domain-runner.service", units)
        self.assertIn("superagent-proactive-hooks-sample_domain.service", units)
        self.assertIn("superagent-proactive-hooks-sample_domain.timer", units)
        self.assertIn("superagent-memory-sample_domain.service", units)

        worker_content = units["superagent-worker-sample_domain-runner.service"]
        self.assertIn('Environment="DEV_MODEL_ROLE=runner"', worker_content)
        self.assertIn('Environment="DEV_MODEL_TIER=pro"', worker_content)
        self.assertIn('Environment="EXTRA_FLAG=1"', worker_content)
        self.assertIn("--max-file 104857600", worker_content)

        timer_content = units["superagent-proactive-hooks-sample_domain.timer"]
        self.assertIn("OnCalendar=*:00/15", timer_content)
        self.assertIn("Persistent=true", timer_content)

        hooks_content = units["superagent-proactive-hooks-sample_domain.service"]
        self.assertIn("-m agentcore.proactive_hooks", hooks_content)

    def test_load_from_dict_and_file(self) -> None:
        raw_dict = {
            "profile_id": "test_qa",
            "version": "1.2.0",
            "description": "QA automation profile",
            "workers": [
                {
                    "role": "ops",
                    "model_tier": "flash",
                    "workdir": "/tmp/qa_workdir",
                    "concurrency": 2,
                }
            ],
            "proactive_hooks": {
                "config_file": "hooks.json",
                "timer_schedule": "hourly",
            },
        }

        spec = load_profile_spec(raw_dict)
        self.assertEqual(spec.profile_id, "test_qa")
        self.assertEqual(spec.version, "1.2.0")
        self.assertEqual(len(spec.workers), 1)
        self.assertEqual(spec.workers[0].concurrency, 2)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir) / "profile.json"
            tmp_path.write_text(json.dumps(raw_dict), encoding="utf-8")

            loaded = load_profile_file(tmp_path)
            self.assertEqual(loaded.profile_id, "test_qa")
            self.assertEqual(loaded.workers[0].role, "ops")

    def test_package_profile_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            profile_yaml = root / "profile.json"
            hooks_json = root / "hooks.json"
            mem_file = root / "memories" / "MEMORY.md"
            mem_file.parent.mkdir(parents=True)

            hooks_json.write_text('{"hooks": []}', encoding="utf-8")
            mem_file.write_text("# Domain Memory", encoding="utf-8")

            raw_dict = {
                "profile_id": "bundle_test",
                "workers": [{"role": "worker_a"}],
                "proactive_hooks": {"config_file": "hooks.json"},
                "memory": {"source": "memories/MEMORY.md"},
            }
            profile_yaml.write_text(json.dumps(raw_dict), encoding="utf-8")

            out_tar = root / "dist" / "bundle.tar.gz"
            packed = package_profile_bundle(profile_yaml, out_tar)
            self.assertTrue(packed.exists())

            with tarfile.open(packed, "r:gz") as tar:
                names = set(tar.getnames())
                self.assertIn("profile.yaml", names)
                self.assertIn("hooks.json", names)
                self.assertIn("memories/MEMORY.md", names)

    def test_cli_dry_run_install_and_inspect(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            profile_json = root / "profile.json"
            profile_json.write_text(
                json.dumps({
                    "profile_id": "cli_profile",
                    "workers": [{"role": "cli_worker", "workdir": str(root)}],
                }),
                encoding="utf-8",
            )
            unit_dir = root / "systemd"

            cmd_inspect = [
                sys.executable, "-m", "install.setup_profile", "inspect", str(profile_json)
            ]
            res = subprocess.run(cmd_inspect, capture_output=True, text=True, check=True)
            self.assertIn("cli_profile", res.stdout)
            self.assertIn("superagent-worker-cli_profile-cli_worker.service", res.stdout)

            cmd_install_dry = [
                sys.executable, "-m", "install.setup_profile", "install",
                str(profile_json), "--unit-dir", str(unit_dir), "--dry-run"
            ]
            res = subprocess.run(cmd_install_dry, capture_output=True, text=True, check=True)
            self.assertIn("[DRY-RUN]", res.stdout)
            self.assertFalse(unit_dir.exists())

            cmd_install_real = [
                sys.executable, "-m", "install.setup_profile", "install",
                str(profile_json), "--unit-dir", str(unit_dir)
            ]
            res = subprocess.run(cmd_install_real, capture_output=True, text=True, check=True)
            self.assertIn("[OK] Installed 1 unit files", res.stdout)
            self.assertTrue((unit_dir / "superagent-worker-cli_profile-cli_worker.service").exists())


if __name__ == "__main__":
    unittest.main()
