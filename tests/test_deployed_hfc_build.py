"""The deployed HFC artifact must match its recorded identity.

Skips when no deployed venv is present, so clean-room and CI runs stay portable.
This test never talks to Feishu and never imports the deployed package: it only
reads distribution metadata and greps installed source for repair signatures.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import urllib.parse

import pytest

REPO = Path(__file__).resolve().parents[1]
LOCK = REPO / "integrations" / "hfc" / "lock.json"
DEFAULT_VENV = Path.home() / ".hermes" / "tools" / "hfc-shared-final-20261102" / "venv"

# Markers that only exist once the reviewed repair is actually in the artifact.
REPAIR_SIGNATURES = {
    "server.py": ["terminal_delivery_state", 'outcome="not_sent"'],
    "feishu_client.py": ["delivery_uuid"],
}


def _venv():
    configured = os.environ.get("HFC_DEPLOYED_VENV")
    venv = Path(configured) if configured else DEFAULT_VENV
    if not venv.is_dir():
        pytest.skip("no deployed HFC venv; set HFC_DEPLOYED_VENV to check one")
    return venv


def _site_packages(venv: Path) -> Path:
    candidates = sorted(venv.glob("lib/python3*/site-packages"))
    if not candidates:
        pytest.skip("deployed venv has no site-packages")
    return candidates[-1]


def _deployed_build() -> dict:
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    record = lock.get("deployed_build")
    if not isinstance(record, dict):
        pytest.fail("lock.json declares no deployed_build; HFC cannot be the sole producer without one")
    return record


def _direct_url(venv: Path) -> dict:
    python = venv / "bin" / "python"
    if not python.is_file():
        pytest.skip("deployed venv has no interpreter")
    probe = (
        "from importlib.metadata import distribution;"
        "print(distribution('hermes-feishu-streaming-card').read_text('direct_url.json') or '{}')"
    )
    proc = subprocess.run([str(python), "-c", probe], text=True, capture_output=True, timeout=30)
    if proc.returncode != 0:
        pytest.skip("deployed venv cannot report distribution metadata")
    return json.loads(proc.stdout)


def test_recorded_wheel_hash_matches_the_installed_artifact():
    record = _deployed_build()
    venv = _venv()
    meta = _direct_url(venv)
    archive = meta.get("archive_info") or {}
    recorded = str(record.get("wheel_sha256") or "")
    installed = str(archive.get("hash") or "")
    if not installed and meta.get("url", "").startswith("file://"):
        url_path = urllib.parse.unquote(meta["url"][len("file://"):])
        wheel_file = Path(url_path)
        if wheel_file.is_file():
            installed = "sha256=" + hashlib.sha256(wheel_file.read_bytes()).hexdigest()
    assert recorded.startswith("sha256=") is False
    assert installed == "sha256=" + recorded, (
        "installed wheel %s does not match recorded %s; re-record before claiming evidence"
        % (installed or "<none>", recorded or "<none>")
    )


def test_recorded_version_matches_the_installed_artifact():
    record = _deployed_build()
    venv = _venv()
    python = venv / "bin" / "python"
    if not python.is_file():
        pytest.skip("deployed venv has no interpreter")
    proc = subprocess.run(
        [str(python), "-c",
         "from importlib.metadata import version;"
         "print(version('hermes-feishu-streaming-card'))"],
        text=True, capture_output=True, timeout=30,
    )
    if proc.returncode != 0:
        pytest.skip("deployed venv cannot report version")
    assert proc.stdout.strip() == record["version"]


def test_reviewed_repair_is_actually_present_in_the_artifact():
    _deployed_build()
    venv = _venv()
    root = _site_packages(venv) / "hermes_feishu_card"
    if not root.is_dir():
        pytest.skip("deployed venv has no hermes_feishu_card package")
    for name, markers in REPAIR_SIGNATURES.items():
        path = root / name
        if not path.is_file():
            pytest.skip("deployed package layout differs from the recorded one")
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in markers:
            assert marker in text, "%s lacks repair marker %r; the wheel is unpatched" % (name, marker)


def test_every_known_blocker_has_recorded_evidence_on_the_deployed_build():
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    blockers = lock.get("known_delivery_blockers")
    record = _deployed_build()
    resolved = record.get("blockers_resolved_on_this_build")
    assert isinstance(blockers, list) and isinstance(resolved, list)
    missing = sorted(set(blockers) - set(resolved))
    assert not missing, "no evidence recorded for %s" % ", ".join(missing)
    covered = (record.get("evidence") or {}).get("covers")
    assert isinstance(covered, list) and not (set(blockers) - set(covered))
    assert (record.get("evidence") or {}).get("result", "").strip(), "evidence result is empty"


def test_the_record_never_claims_interaction_acceptance():
    record = _deployed_build()
    not_verified = record.get("not_verified")
    assert isinstance(not_verified, list) and not_verified, (
        "a deployed build that lists no open gaps is claiming full acceptance; "
        "automated contracts do not replace a real turn"
    )
    assert record.get("role") == "single interaction producer"
