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

Exit code = number of failed checks; a skipped check is reported, never counted as pass.
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

    def ok(self, name, detail=""):
        print("  [PASS] %s%s" % (name, "" if not detail else ": " + detail))

    def bad(self, name, detail=""):
        self.failures += 1
        print("  [FAIL] %s%s" % (name, "" if not detail else ": " + detail))

    def skip(self, name, why):
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
    args = parser.parse_args(argv)
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
    print("\n== %s" % ("all checks passed" if not result.failures
                       else "%d check(s) failed" % result.failures))
    return result.failures


if __name__ == "__main__":
    raise SystemExit(main())
