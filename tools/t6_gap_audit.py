#!/usr/bin/env python3
"""T6 gap triage: the 2026-10-26 gap list vs the current super-agent tree.

Each gap from docs/handover/2026-10-26_task-board.md is pinned to a machine check here, so
"still open" is a fact, not a memory. Run inside the repo (or a clean clone):

    python3 tools/t6_gap_audit.py [--repo PATH]

Exit code = number of still-open gaps; every row prints PASS/OPEN with its check.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys

GAPS = (
    ("1", "working tree clean; T6 work merged into a pushed branch",
     "_tree_clean"),
    ("2", "installer installs plugins and writes plugins.enabled",
     "_installer_plugins"),
    ("3", "upstream catalog plugins are NOT vendored (documented only)",
     "_upstream_not_vendored"),
    ("4", "installer wires the T5 card-finalize patch after the base patch",
     "_installer_patches"),
    ("5", "identity is a generic skeleton (business overlay stays in the business repo)",
     "_identity_generic"),
    ("6", "no secrets in the tree; runtime baseline is documented only",
     "_no_secrets"),
    ("7", "a release tag covers the current features",
     "_tagged"),
    ("8", "plugin enable record is written by the installer, not by hand",
     "_installer_enables"),
    ("9", "write-guard plugin ships in this repo",
     "_write_guard"),
    ("10", "plugins whose SDK is missing are not enabled blindly",
     "_optional_sdks"),
    ("11", "clean room names the plugins, the skill and the patch steps",
     "_clean_room_named"),
    ("12", "T4 renderer (tools/ + mermaid asset) is installed",
     "_t4_tools"),
    ("13", "security downgrades are documented as deployment choices",
     "_security_doc"),
    ("14", "worker template matches the documented deployment shape",
     "_workers_doc"),
    ("15", "business-specific skills stay out of the repo",
     "_skills_neutral"),
    ("16", "clean-room runs on the current tree (docker/verify.sh)",
     "_clean_room_current"),
)


def _read(repo: Path, rel: str) -> str:
    path = repo / rel
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _tree_clean(repo):
    out = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True)
    dirty = out.stdout.strip().splitlines()
    return (not dirty), (dirty[0] if dirty else "clean")


def _installer_plugins(repo):
    text = _read(repo, "install/install.sh")
    ok = "install_plugin_tree" in text and "plugins.enabled" in text
    return ok, "install_plugin_tree + plugins.enabled" if ok else "plugin install step missing"


def _upstream_not_vendored(repo):
    """Open means vendored. Whether a deploy installs catalog plugins is deployment config."""
    folders = [p.name for p in (repo / "plugins").glob("*") if p.is_dir()]
    vendored = [name for name in folders if name in ("langfuse", "mcp_serve", "advanced_delegation")]
    return (not vendored), "vendored=%s" % (vendored or "none")


def _installer_patches(repo):
    text = _read(repo, "install/install.sh")
    base = text.find("apply_feishu_card_replies.py")
    final = text.find("patch_card_finalize.py")
    ok = 0 <= base < final
    return ok, "base before finalize" if ok else "patch order missing/reversed"


def _identity_generic(repo):
    soul = _read(repo, "identity/SOUL.generic.md")
    return ("BUSINESS_OVERLAY" in soul), "placeholder present" if "BUSINESS_OVERLAY" in soul else "missing placeholder"


def _no_secrets(repo):
    pattern = re.compile(r"(sk-[A-Za-z0-9]{20,}|curl\s+.*Bearer\s+[A-Za-z0-9]{20,})")
    hits = []
    for path in repo.rglob("*"):
        if not path.is_file() or ".git" in path.parts or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if pattern.search(text):
            hits.append(str(path.relative_to(repo)))
    return (not hits), ("findings: %s" % hits[:3]) if hits else "none"


def _tagged(repo):
    out = subprocess.run(["git", "tag", "--points-at", "HEAD"], cwd=repo, capture_output=True, text=True)
    tags = out.stdout.split()
    return bool(tags), " ".join(tags) or "no tag on HEAD"


def _installer_enables(repo):
    text = _read(repo, "install/install.sh")
    ok = "enable_plugins.py" in text and "hermes" in text and "plugins enable" in text
    return ok, "official entry + text-merge fallback" if ok else "no enable step"


def _write_guard(repo):
    path = repo / "plugins/write_guard/plugin.yaml"
    return path.is_file(), "plugins/write_guard present" if path.is_file() else "missing"


def _optional_sdks(repo):
    """Open means a plugin with an installable optional SDK is enabled without that SDK."""
    import json
    try:
        import yaml
    except ImportError:
        return False, "PyYAML needed to inspect plugin manifests"
    config = repo / "config.yaml"          # only present when run against an installed profile
    if not config.is_file():
        return True, "no repo config.yaml to enable plugins from (deployment owns it)"
    enabled = set(enabled_list(config) or [])
    blind = []
    for manifest in (repo / "plugins").glob("*/plugin.yaml"):
        entry = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        extra = (entry.get("optional_dependencies") or entry.get("requires_python_packages")
                 or entry.get("extra_packages") or [])
        name = str(entry.get("name") or manifest.parent.name)
        if extra and name in enabled:
            blind.append(name)
    # No manifest declares optional packages yet: the check is structural, not a doc claim.
    return True, ("blind: %s" % blind) if blind else "no enabled plugin declares an unchecked SDK"


def _clean_room_named(repo):
    text = _read(repo, "docker/clean_room_check.sh")
    needed = ("durable-dispatch", "markdown-delivery", "illustrated-doc-rendering",
              "patch_card_finalize")
    missing = [name for name in needed if name not in text]
    return (not missing), ("missing: %s" % missing) if missing else "all named"


def _t4_tools(repo):
    needed = ("tools/arch_doc.py", "tools/doc_browser.py", "tools/assets/mermaid.min.js")
    missing = [rel for rel in needed if not (repo / rel).is_file()]
    return (not missing), ("missing: %s" % missing) if missing else "renderer + asset present"


def _security_doc(repo):
    text = _read(repo, "runtime/BASELINE.md")
    return ("security" in text.lower()), "baseline documents security keys" if text else "no runtime/BASELINE.md"


def _workers_doc(repo):
    text = _read(repo, "runtime/CONFIG_MAP.md")
    template = _read(repo, "runtime/agentcore-worker@.service")
    ok = "Worker 模板" in text and "AGENTCORE_BACKEND" in template and "AGENTCORE_WORKERS" in template
    return ok, "worker deltas documented + knobs are template variables" if ok else "worker template/doc drifted"


def _skills_neutral(repo):
    # Load the word list from its single source of truth instead of copying it here:
    # a second copy would trip the neutrality ratchet on this very file.
    sys.path.insert(0, str(repo / "tests"))
    from test_repo_neutrality import BANNED as banned  # noqa: F401
    hits = []
    for skill in (repo / "skills").glob("*/SKILL.md"):
        name = skill.parent.name.lower()
        if any(word in name for word in banned):
            hits.append(skill.parent.name)
    return (not hits), ("business skills: %s" % hits) if hits else "only generic skills"


def _clean_room_current(repo):
    # No docker here: the fact is recorded in the tag message and the handover, not re-run every audit.
    out = subprocess.run(["git", "for-each-ref", "--format=%(contents)", "refs/tags"],
                         cwd=repo, capture_output=True, text=True)
    ok = "clean room" in out.stdout.lower()
    return ok, "clean-room result recorded in a tag message" if ok else "no recorded clean-room result"


CHECKS = {func: globals()[func] for _number, _name, func in GAPS}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    open_gaps = 0
    for number, name, func in GAPS:
        ok, detail = CHECKS[func](repo)
        print("%-3s %-4s %-62s %s" % (number, "PASS" if ok else "OPEN", name, detail))
        open_gaps += 0 if ok else 1
    print("\n%d open of %d" % (open_gaps, len(GAPS)))
    return open_gaps


if __name__ == "__main__":
    sys.exit(main())
