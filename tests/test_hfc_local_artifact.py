"""Recover a missing wheel digest only from a matching installed payload."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import zipfile

import pytest

spec = importlib.util.spec_from_file_location("hfc_gate", Path(__file__).resolve().parents[1] / "tools/hfc_acceptance.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def _installed(tmp_path, monkeypatch):
    root = tmp_path / "site"
    info = root / "hermes_feishu_streaming_card-5.0.13.dist-info"
    info.mkdir(parents=True)
    module = root / "hermes_feishu_card/__init__.py"
    module.parent.mkdir()
    module.write_text("VALUE = 1\n")
    metadata = info / "METADATA"
    metadata.write_text("Name: hermes-feishu-streaming-card\nVersion: 5.0.13\n")
    wheel = tmp_path / "hfc.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path in [module, metadata]:
            archive.write(path, path.relative_to(root).as_posix())
        archive.writestr(info.name + "/RECORD", "")
    (info / "direct_url.json").write_text(json.dumps({"url": wheel.as_uri(), "archive_info": {}}))
    cli = tmp_path / "hfc-cli"
    cli.write_text("#!" + sys.executable + "\n")
    monkeypatch.setenv("PYTHONPATH", str(root))
    return cli, wheel, module


def test_local_wheel_without_metadata_hash_is_verified(tmp_path, monkeypatch):
    cli, wheel, _ = _installed(tmp_path, monkeypatch)
    assert gate.installed_identity(str(cli)) == {"kind": "deployed_wheel", "version": "5.0.13",
                                              "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}


@pytest.mark.parametrize("mutation", ["changed", "missing"])
def test_local_wheel_cannot_certify_changed_installed_code(tmp_path, monkeypatch, mutation):
    cli, _, module = _installed(tmp_path, monkeypatch)
    if mutation == "changed":
        module.write_text("VALUE = 2\n")
    else:
        module.unlink()
    assert gate.installed_identity(str(cli)) is None
