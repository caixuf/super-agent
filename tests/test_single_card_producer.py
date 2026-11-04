"""HFC must stay the only interaction producer.

Two card producers on one chat is the failure mode this repository already paid
for: both write, neither owns the final answer, and "delivered" becomes a guess.
This gate is deliberately mechanical -- it does not prove delivery quality, only
that no second producer is sneakily wired back in.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LOCK = REPO / "integrations" / "hfc" / "lock.json"

# Native producers that must never be instantiated by non-test code while HFC owns
# the channel. They remain importable libraries; wiring them is the forbidden act.
FORBIDDEN_INSTANTIATIONS = ("FeishuCardKitAdapter(", "CardStreamRenderer(")

# Files allowed to mention the classes: their own definition site, and the doc
# tables that must keep calling them "not wired".
DEFINITION_SITES = {"agentcore/feishu_transport.py", "agentcore/cardkit2_renderer.py"}


def _sources():
    for path in REPO.rglob("*.py"):
        relative = path.relative_to(REPO).as_posix()
        if relative.startswith(("tests/", ".venv/", "build/", "dist/")):
            continue
        if "__pycache__" in relative:
            continue
        yield relative, path


def test_no_native_card_producer_is_instantiated_outside_tests():
    offenders = []
    for relative, path in _sources():
        if relative in DEFINITION_SITES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in FORBIDDEN_INSTANTIATIONS:
            if needle in text:
                offenders.append("%s -> %s" % (relative, needle.rstrip("(")))
    assert not offenders, (
        "a second card producer was wired in: %s. HFC owns the channel; "
        "wire a new renderer only after retiring HFC in the same change."
        % ", ".join(sorted(offenders))
    )


def test_lock_declares_a_single_interaction_producer():
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    record = lock.get("deployed_build") or {}
    assert record.get("role") == "single interaction producer"
    assert record.get("wheel_sha256"), "the producer must be identified by artifact, not by name"
    assert lock.get("ownership") == "upstream_cli_and_sidecar"


def test_docs_keep_the_native_producers_marked_unwired():
    docs = (REPO / "docs" / "README.md").read_text(encoding="utf-8")
    for name in ("FeishuCardKitAdapter", "CardStreamRenderer"):
        assert name in docs, (
            "%s is no longer listed as unwired in docs/README.md; "
            "either it really is wired (then retire HFC) or the record drifted" % name
        )
