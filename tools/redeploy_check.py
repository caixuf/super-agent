#!/usr/bin/env python3
"""Re-deploy gate for the super-agent repo: every item must pass on a clean clone.

    git clone https://github.com/caixuf/super-agent.git /tmp/sa && cd /tmp/sa
    python3 tools/redeploy_check.py [--ref origin/master] [--skip-docker]

Checks, in the order a new machine hits them:

1. the clone is clean (no uncommitted or untracked files);
2. every plugin manifest loads from the installed tree and appears in ``plugins.enabled``
   of a fresh target profile (fresh install, not dry-run);
3. both Feishu patches apply to a pristine Hermes export and pass ``--check`` afterwards;
4. ``verify.sh`` exits 0 (static + unit + installer dry-run);
5. with ``--docker``: ``docker/verify.sh <ref>`` (clean-room) exits 0.

These repository checks do not certify production runtime readiness. For a bounded,
read-only profile report use ``--readiness-profile HOME [--json]``. Full readiness
fails closed until runtime evidence exists; ``--configuration-only`` checks static
prerequisites only and must not be used as a production acceptance gate.

Repository mode exit code = number of failed checks; skips never count as passes.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]


class Result:
    def __init__(self):
        self.failures = 0
        self.skipped = 0

    def ok(self, name, detail=""):
        print("  [PASS] %s%s" % (name, "" if not detail else ": " + detail))

    def bad(self, name, detail=""):
        self.failures += 1
        print("  [FAIL] %s%s" % (name, "" if not detail else ": " + detail))

    def skip(self, name, why):
        self.skipped += 1
        print("  [SKIP] %s (%s)" % (name, why))


def run(cmd, *, cwd=None, env=None, timeout=1800):
    return subprocess.run(cmd, cwd=str(cwd or REPO), env=env, text=True,
                          capture_output=True, timeout=timeout)


def plugin_names():
    names = []
    for manifest in sorted((REPO / "plugins").glob("*/plugin.yaml")):
        text = manifest.read_text(encoding="utf-8")
        match = re.search(r"(?m)^name:\s*\"?([^\"\n]+)\"?\s*$", text)
        if match:
            names.append(match.group(1).strip())
    return names


def profile_readiness(home: Path):
    """Inspect declared static prerequisites, never import or start profile plugins.

    Deliberately does not open registries/queues or call external services. Settings,
    secrets and YAML errors are not copied into this machine-consumable report.
    Even a complete static configuration is not tool registration, dispatcher
    bootstrap, worker readiness or successful end-to-end delivery.
    """
    report = {"schema_version": 1, "scope": "static_prerequisites",
              "configuration_ready": False, "runtime_ready": False,
              "state": "blocked", "plugins": [], "blockers": [],
              "runtime_blockers": ["runtime_tool_registration_unverified",
                                   "end_to_end_delivery_unverified",
                                   "colleague_dispatch_production_backend_unavailable"]}
    try:
        import yaml
        config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    except ImportError:
        report["blockers"].append("PyYAML_required_for_readiness")
        return report
    except (OSError, ValueError, yaml.YAMLError):
        report["blockers"].append("profile_config_missing_or_invalid")
        return report
    if not isinstance(config, dict) or not isinstance(config.get("plugins"), dict):
        report["blockers"].append("plugins_mapping_required")
        return report
    plugins = config["plugins"]
    enabled = plugins.get("enabled", [])
    disabled = plugins.get("disabled", [])
    entries = plugins.get("entries", {})
    if (not isinstance(enabled, list) or not all(isinstance(x, str) for x in enabled)
            or not isinstance(disabled, list) or not all(isinstance(x, str) for x in disabled)
            or not isinstance(entries, dict)):
        report["blockers"].append("plugin_enable_lists_and_entries_invalid")
        return report
    for manifest in sorted((REPO / "plugins").glob("*/plugin.yaml")):
        match = re.search(r'(?m)^name:\s*"?([^"\n]+)"?\s*$', manifest.read_text())
        if not match:
            report["blockers"].append("plugin_manifest_name_invalid")
            continue
        name = match.group(1).strip()
        source = (manifest.parent / "__init__.py").read_text()
        gated = re.search(r"get_config\(['\"]enabled['\"]", source) is not None
        entry = entries.get(name, {})
        settings = entry.get("settings", {}) if isinstance(entry, dict) else {}
        issues = []
        installed = (home / "plugins" / name / "__init__.py").is_file()
        listed = name in enabled and name not in disabled
        if not installed:
            issues.append("plugin_not_installed")
        if not listed:
            issues.append("plugin_not_enabled")
        if not isinstance(entry, dict) or not isinstance(settings, dict):
            issues.append("plugin_settings_mapping_required")
            settings = {}
        if entry.get("enabled") is False if isinstance(entry, dict) else False:
            issues.append("plugin_entry_explicitly_disabled")
        if gated and settings.get("enabled") is not True:
            issues.append("settings.enabled_required_true")

        # Only check prerequisites with an explicit source contract; no profile
        # code is imported, no settings value or credential is printed.
        if gated and settings.get("enabled") is True:
            if name in {"colleague-catalog", "colleague-team", "durable-dispatch",
                        "illustrated-delivery", "markdown-delivery", "memory-bridge",
                        "task-progress", "task-status"}:
                root = settings.get("engine_root")
                if not isinstance(root, str) or not Path(root).is_absolute() or not (Path(root) / "agentcore").is_dir():
                    issues.append("absolute_engine_root_with_agentcore_required")
            if name == "colleague-team":
                for key in ("team_id", "primary_id"):
                    if not isinstance(settings.get(key), str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", settings[key]):
                        issues.append(key + "_valid_identity_required")
                registry = settings.get("registry_path")
                if not isinstance(registry, str) or not Path(registry).is_absolute():
                    issues.append("absolute_registry_path_required")
                else:
                    path = Path(registry)
                    if path.is_symlink() or not path.parent.is_dir():
                        issues.append("private_registry_parent_required")
                    elif path.parent.stat().st_mode & 0o077 or path.parent.stat().st_uid != os.getuid():
                        issues.append("private_registry_parent_required")
            if name == "colleague-catalog":
                roles = settings.get("roles")
                if not isinstance(roles, dict) or not roles:
                    issues.append("administrator_catalog_roles_required")
            if name == "durable-dispatch":
                queue = settings.get("queue_root")
                if not isinstance(queue, str) or not Path(queue).is_absolute():
                    issues.append("absolute_queue_root_required")
                roots = settings.get("allowed_roots")
                if (not isinstance(roots, list) or not roots
                        or any(not isinstance(x, str) or not Path(x).is_absolute() or not Path(x).is_dir() for x in roots)):
                    issues.append("explicit_existing_allowed_roots_required")
            if name == "memory-bridge":
                spool = settings.get("spool_dir")
                if not isinstance(spool, str) or not Path(spool).is_absolute():
                    issues.append("absolute_spool_dir_required")
                for key in ("user_id", "agent_id"):
                    if not isinstance(settings.get(key), str) or not settings[key].strip():
                        issues.append(key + "_required")
            if name == "local-model-grounding" and not settings.get("gateway_url"):
                issues.append("gateway_url_required")
            if name == "markdown-delivery":
                for key in ("outbox_root", "env_file"):
                    value = settings.get(key)
                    if not isinstance(value, str) or not Path(value).is_absolute():
                        issues.append("absolute_" + key + "_required")
        report["plugins"].append({"name": name, "installed": installed,
                                  "enabled": listed, "settings_gated": gated,
                                  "configuration_state": "blocked" if issues else "configured",
                                  "runtime_state": "unverified", "blockers": issues})
    report["configuration_ready"] = not report["blockers"] and all(
        p["configuration_state"] == "configured" for p in report["plugins"])
    if not report["plugins"]:
        report["blockers"].append("no_plugin_manifests_found")
        report["configuration_ready"] = False
    if report["configuration_ready"]:
        report["state"] = "configuration_ready_runtime_unverified"
    return report


def enabled_list(config_path: Path):
    """``plugins.enabled`` as installed, read the way the config itself is structured."""
    if not config_path.is_file():
        return None
    try:
        import yaml
    except ImportError:  # tiny reader: the list items are 2- or 4-space indented children
        lines = config_path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if line.startswith("  enabled:") and not line.startswith("    "):
                found = []
                for follower in lines[index + 1:]:
                    stripped = follower.strip()
                    if stripped.startswith("- ") and follower.startswith(("  ", "    ")):
                        found.append(stripped[2:].strip("\"'"))
                    elif stripped and not follower.startswith(" "):
                        break
                return found
        return None
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return list((data.get("plugins") or {}).get("enabled") or [])


def check_clone(result, ref):
    print("\n== ① clone cleanliness")
    status = run(["git", "status", "--porcelain"]).stdout.strip()
    if status:
        result.bad("clone is clean", status.splitlines()[0])
    else:
        result.ok("clone is clean")
    head = run(["git", "rev-parse", "--short", "HEAD"]).stdout.strip()
    target = run(["git", "rev-parse", "--short", ref]).stdout.strip()
    if head and head == target:
        result.ok("HEAD equals " + ref, head)
    else:
        result.bad("HEAD equals " + ref, "%s != %s" % (head, target))


def check_install(result):
    print("\n== ② fresh profile install + plugin enable list", flush=True)
    with tempfile.TemporaryDirectory() as home:
        env = {k: v for k, v in os.environ.items()
               if k not in ("HERMES_HOME", "HERMES_AGENT_DIR", "PLUGIN_ENABLE_MODE")}
        proc = run(["bash", str(REPO / "install/install.sh"), "--hermes-home", home, "--skip-patch"],
                   env=env)
        if proc.returncode != 0:
            result.bad("installer exits 0 on a fresh profile", proc.stderr.strip()[-300:])
            return
        result.ok("installer exits 0 on a fresh profile")
        tree = Path(home) / "plugins"
        expected = plugin_names()
        missing = [name for name in expected if not (tree / name / "__init__.py").is_file()]
        if missing:
            result.bad("every manifest plugin lands", ", ".join(missing))
        else:
            result.ok("every manifest plugin lands", "%d plugins" % len(expected))
        enabled = enabled_list(Path(home) / "config.yaml") or []
        not_enabled = [name for name in expected if name not in enabled]
        if not_enabled:
            result.bad("every installed plugin is enabled", ", ".join(not_enabled))
        else:
            result.ok("every installed plugin is enabled")


def check_patches(result):
    print("\n== ③ Feishu patches on a pristine Hermes export")
    agent = Path(os.path.expanduser(os.environ.get("HERMES_AGENT_DIR", "~/.hermes/hermes-agent")))
    if not (agent / "gateway").is_dir():
        result.skip("patch sequence", "no Hermes source tree at %s" % agent)
        return
    files = ["gateway/stream_consumer.py", "gateway/stream_consumer_transport.py",
             "plugins/platforms/feishu/adapter.py"]
    with tempfile.TemporaryDirectory() as scratch:
        tree = Path(scratch)
        for relative in files:
            source = agent / relative
            if not source.is_file():
                result.bad("Hermes tree has " + relative)
                return
            (tree / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, tree / relative)
        env = dict(os.environ, HERMES_AGENT_DIR=str(tree))
        for script in ("apply_feishu_card_replies.py", "patch_card_finalize.py",
                       "patch_card_markdown_native.py", "patch_feishu_update_retry.py"):
            path = REPO / "patches" / script
            check = run([sys.executable, str(path), "--check"], env=env)
            if check.returncode not in (0, 1):
                result.bad(script + " --check crashes", check.stderr.strip()[-200:])
                return
            applied = run([sys.executable, str(path), "--apply"], env=env)
            verified = run([sys.executable, str(path), "--check"], env=env)
            replay = run([sys.executable, str(path), "--apply"], env=env) if applied.returncode == 0 else None
            idempotent = replay is not None and replay.returncode == 0
            if applied.returncode == 0 and verified.returncode == 0 and idempotent:
                result.ok(script + " applies, verifies, re-applies")
            else:
                result.bad(script + " applies and verifies", (applied.stderr or verified.stdout)[-200:])
        uuid = REPO / "patches" / "patch_feishu_delivery_uuid.py"
        cmd = [sys.executable, str(uuid), "--hermes-root", str(tree)]
        applied = run(cmd + ["--apply"])
        verified = run(cmd)
        replay = run(cmd + ["--apply"])
        if applied.returncode == verified.returncode == replay.returncode == 0:
            result.ok("Feishu delivery UUID patch applies, verifies, re-applies")
        else:
            result.bad("Feishu delivery UUID patch", (applied.stderr or verified.stderr or replay.stderr)[-300:])


def check_card_roundtrip(result):
    """The patched adapter really builds the cards we expect (mock, no platform calls).

    Needs HERMES_FEISHU_ADAPTER to point at a patched adapter; the tests skip otherwise.
    """
    print("\n== ③b card round-trip on the patched adapter", flush=True)
    agent = Path(os.path.expanduser(os.environ.get("HERMES_AGENT_DIR", "~/.hermes/hermes-agent")))
    adapter = agent / "plugins/platforms/feishu/adapter.py"
    if not adapter.is_file():
        result.skip("card round-trip", "no patched adapter at %s" % adapter)
        return
    env = dict(os.environ, HERMES_FEISHU_ADAPTER=str(adapter))
    proc = run([sys.executable, "-m", "pytest", "tests/test_card_markdown_native_patch.py",
                "tests/test_card_24_elements.py", "-q"], env=env)
    tail = [line for line in proc.stdout.splitlines() if "passed" in line or "failed" in line]
    if proc.returncode == 0:
        result.ok("card round-trip", tail[-1] if tail else "")
    else:
        result.bad("card round-trip", tail[-1] if tail else proc.stderr[-200:])


def check_verify(result):
    print("\n== ④ repo self-check (verify.sh)", flush=True)
    proc = run(["bash", str(REPO / "verify.sh")], timeout=3600)
    if proc.returncode == 0:
        tail = [line for line in proc.stdout.splitlines() if "passed" in line][-1:]
        result.ok("verify.sh exits 0", tail[0].strip() if tail else "")
    else:
        result.bad("verify.sh exits 0", proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "")


def check_docker(result, ref):
    print("\n== ⑤ clean-room (docker/verify.sh)")
    if not shutil.which("docker"):
        result.skip("docker/verify.sh", "docker not installed")
        return
    proc = run(["bash", str(REPO / "docker/verify.sh"), ref], timeout=5400)
    if proc.returncode == 0:
        result.ok("docker/verify.sh exits 0", ref)
    else:
        result.bad("docker/verify.sh exits 0", proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--skip-docker", action="store_true")
    parser.add_argument("--readiness-profile", type=Path, metavar="HOME",
                        help="read-only static prerequisite report; never starts profile code")
    parser.add_argument("--json", action="store_true", help="machine-readable readiness report")
    parser.add_argument("--configuration-only", action="store_true",
                        help="exit 0 for static prerequisites only, NOT runtime readiness")
    args = parser.parse_args(argv)
    if (args.json or args.configuration_only) and args.readiness_profile is None:
        parser.error("--json and --configuration-only require --readiness-profile")
    if args.readiness_profile is not None:
        report = profile_readiness(args.readiness_profile)
        if args.json:
            print(json.dumps(report, sort_keys=True))
        else:
            print("Profile readiness: " + report["state"])
            for plugin in report["plugins"]:
                print("  %s: %s%s" % (plugin["name"], plugin["configuration_state"],
                      " (" + ", ".join(plugin["blockers"]) + ")" if plugin["blockers"] else ""))
            for blocker in report["blockers"] + report["runtime_blockers"]:
                print("  [BLOCKED] " + blocker)
        return 0 if args.configuration_only and report["configuration_ready"] else 1
    result = Result()
    check_clone(result, args.ref)
    check_install(result)
    check_patches(result)
    check_card_roundtrip(result)
    check_verify(result)
    if args.skip_docker:
        result.skip("docker/verify.sh", "--skip-docker")
    else:
        check_docker(result, args.ref)
    print("\n== repository checks: %d failed, %d skipped; production runtime readiness NOT verified"
          % (result.failures, result.skipped))
    return result.failures


if __name__ == "__main__":
    raise SystemExit(main())
