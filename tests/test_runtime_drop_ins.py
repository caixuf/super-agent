"""Deployment must report failures loading the desired systemd configuration."""
import pytest
import yaml

from test_superagent_installer import _load_installer, _run, _seed_profile


def _fixture(tmp_path, monkeypatch, drop_ins):
    module = _load_installer()
    repo = tmp_path / "repo"
    manifest = repo / "identity/manifests/demo.manifest.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(yaml.safe_dump({"runtime": {"drop_ins": drop_ins}}))
    monkeypatch.setattr(module, "REPO", repo)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return module, tmp_path / "home/.config/systemd/user/hermes-gateway-demo.service.d"


def test_reload_failure_is_reported_and_retried_for_existing_files(tmp_path, monkeypatch):
    module, directory = _fixture(tmp_path, monkeypatch, {"10-card.conf": {"HERMES_FEISHU_CARD_V2": "1"}})
    calls = []

    def failed_reload(argv, **kwargs):
        calls.append((argv, kwargs))
        raise RuntimeError("systemd reload failed")

    monkeypatch.setattr(module, "_run", failed_reload)
    with pytest.raises(RuntimeError, match="systemd reload failed"):
        module._apply_runtime_drop_ins("demo")
    assert (directory / "10-card.conf").is_file()
    # Retry must ask systemd again even though the desired file already exists.
    monkeypatch.setattr(module, "_run", lambda argv, **kwargs: calls.append((argv, kwargs)))
    module._apply_runtime_drop_ins("demo")
    assert len(calls) == 2
    assert all(argv == ["systemctl", "--user", "daemon-reload"] and kwargs.get("check") for argv, kwargs in calls)


@pytest.mark.parametrize("bad", [
    {"../../other.conf": {"KEY": "1"}},
    {"11-invalid.conf": {"KEY": "1\nExecStart=/bin/false"}},
    {"11-invalid.conf": {"KEY": {"nested": "value"}}},
])
def test_invalid_manifest_does_not_partially_change_service(tmp_path, monkeypatch, bad):
    module, directory = _fixture(tmp_path, monkeypatch, {"10-card.conf": {"KEY": "1"}, **bad})
    monkeypatch.setattr(module, "_run", lambda *a, **kw: pytest.fail("must not reload invalid configuration"))
    with pytest.raises(ValueError):
        module._apply_runtime_drop_ins("demo")
    assert not directory.exists()


def test_systemd_environment_preserves_literal_values(tmp_path, monkeypatch):
    module, directory = _fixture(tmp_path, monkeypatch, {"10-value.conf": {"KEY": '100% "value"'}})
    monkeypatch.setattr(module, "_run", lambda *a, **kw: None)
    module._apply_runtime_drop_ins("demo")
    assert (directory / "10-value.conf").read_text() == '[Service]\n' + r'Environment="KEY=100%% \"value\""' + '\n'


def test_explicit_manifest_works_without_ignored_repo_manifest(tmp_path, monkeypatch):
    module, directory = _fixture(tmp_path, monkeypatch, {"10-card.conf": {"KEY": "1"}})
    manifest = module.REPO / "identity/manifests/demo.manifest.yaml"
    external = tmp_path / "host-runtime.yaml"
    manifest.rename(external)
    monkeypatch.setattr(module, "_run", lambda *a, **kw: None)
    assert module._apply_runtime_drop_ins("demo") == {"status": "not_configured"}
    result = module._apply_runtime_drop_ins("demo", manifest_path=external)
    assert result["status"] == "applied"
    assert result["manifest_sha256"]
    assert (directory / "10-card.conf").is_file()


def test_explicit_missing_manifest_fails_instead_of_silently_skipping(tmp_path):
    module = _load_installer()
    with pytest.raises(FileNotFoundError, match="explicit runtime manifest is missing"):
        module._apply_runtime_drop_ins("demo", manifest_path=tmp_path / "missing.yaml")


def test_missing_manifest_blocks_install_before_any_managed_writes(tmp_path):
    _seed_profile(tmp_path)
    result = _run(tmp_path, "install", "--runtime-manifest", str(tmp_path / "missing.yaml"))
    assert result.returncode != 0
    assert "runtime_manifest_missing" in result.stdout
    assert not (tmp_path / ".hermes/superagent").exists()
