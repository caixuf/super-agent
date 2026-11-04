"""Tests for agentcore.artifact_retention."""

from __future__ import annotations

import time
from pathlib import Path
import pytest

from agentcore.artifact_retention import (
    ArtifactRetentionManager,
    DeliveryFilterPolicy,
)


def test_delivery_filter_policy():
    policy = DeliveryFilterPolicy(allow_html=True, allow_screenshots=False)
    # HTML must be delivered
    assert policy.should_deliver_file("report_mff_hang_method.html") is True
    # Screenshots must be rejected
    assert policy.should_deliver_file("report_mff_hang_method.png") is False
    assert policy.should_deliver_file("preview.jpg") is False
    # Other files (like json / log) can be delivered
    assert policy.should_deliver_file("metrics.json") is True


def test_artifact_retention_prune_expired(tmp_path: Path):
    manager = ArtifactRetentionManager(root_dir=tmp_path, ttl_seconds=100.0)
    manager.ensure_directory()

    now = time.time()
    # File 1: Old file (150s ago, should be pruned)
    old_file = tmp_path / "old_report.html"
    old_file.write_text("old html content")
    os_time_old = now - 150.0
    import os
    os.utime(old_file, (os_time_old, os_time_old))

    # File 2: New file (20s ago, should be kept)
    new_file = tmp_path / "new_report.html"
    new_file.write_text("new html content")
    os_time_new = now - 20.0
    os.utime(new_file, (os_time_new, os_time_new))

    summary = manager.prune_expired(now=now)
    assert summary.scanned_count == 2
    assert summary.removed_count == 1
    assert not old_file.exists()
    assert new_file.exists()
