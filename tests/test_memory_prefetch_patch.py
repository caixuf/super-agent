import importlib.util
from pathlib import Path

import pytest


def test_observation_patch_is_idempotent_and_refuses_anchor_drift():
    path = Path(__file__).resolve().parents[1]/'patches/patch_memory_prefetch_observation.py'
    spec = importlib.util.spec_from_file_location('prefetch_patch_test', path)
    patch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(patch)
    source = 'def prefetch():\n' + patch.OLD
    result = patch.transform(source)
    compile(result, 'patched-prefetch', 'exec')
    assert patch.transform(result) == result
    assert 'type(exc).__name__' in result
    with pytest.raises(ValueError, match='anchor drifted'):
        patch.transform('changed upstream source')
