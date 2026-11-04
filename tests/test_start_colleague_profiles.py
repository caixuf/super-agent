import importlib.util
from pathlib import Path
from subprocess import CompletedProcess

import pytest


spec = importlib.util.spec_from_file_location(
    "start_colleague_profiles", Path(__file__).parents[1] / "runtime/start_colleague_profiles.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_starts_each_gateway_only_after_preceding_identity(tmp_path):
    for name in ("colleague-test", "colleague-cases"):
        (tmp_path / name).mkdir()
    events = []

    def run(command, **_kwargs):
        events.append(("start", command[-1]))
        return CompletedProcess(command, 0)

    def identify(home, **_kwargs):
        events.append(("identify", home.name))
        return {"served_profiles": [{"home": str(home)}]}

    module.start_profiles(["colleague-test", "colleague-cases"], profiles_root=tmp_path, identify=identify, run=run)
    assert events == [("start", "hermes-gateway-colleague-test.service"), ("identify", "colleague-test"),
                      ("start", "hermes-gateway-colleague-cases.service"), ("identify", "colleague-cases")]


def test_does_not_start_followers_when_previous_identity_is_unknown(tmp_path):
    for name in ("colleague-test", "colleague-cases"):
        (tmp_path / name).mkdir()
    started = []
    def run(command, **_kwargs):
        started.append(command[-1])
        return CompletedProcess(command, 0)
    with pytest.raises(RuntimeError, match="profile_identity_unconfirmed"):
        module.start_profiles(["colleague-test", "colleague-cases"], profiles_root=tmp_path,
                              identify=lambda *_args, **_kwargs: {}, run=run, timeout=0)
    assert started == ["hermes-gateway-colleague-test.service"]
