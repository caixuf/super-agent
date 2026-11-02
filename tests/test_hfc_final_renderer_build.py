"""Wheel overlay is reproducible, pinned and does not change delivery ownership."""
import base64
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('hfc_final_build', ROOT / 'tools/build_hfc_final_renderer.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


def base_wheel(tmp_path, server=None):
    p = tmp_path / 'hermes_feishu_streaming_card-5.0.13-py3-none-any.whl'
    with zipfile.ZipFile(p, 'w') as z:
        z.writestr(build.SERVER_PATH, server or ('def seam(card_config, result):\n' + build.RENDER_SEAM + '\n        pass\n'))
        z.writestr('hermes_feishu_card/config_rules.py', build.CONFIG_SEAM + '    return\n')
        z.writestr('hermes_feishu_card/hook_runtime.py', 'DELIVERY_MUST_NOT_CHANGE = True\n')
        z.writestr('hermes_feishu_streaming_card-5.0.13.dist-info/METADATA',
                   'Name: hermes-feishu-streaming-card\nVersion: 5.0.13\n')
        z.writestr('hermes_feishu_streaming_card-5.0.13.dist-info/RECORD', '')
    return p, hashlib.sha256(p.read_bytes()).hexdigest()


def test_deterministic_overlay_and_single_source(tmp_path):
    p, sha = base_wheel(tmp_path)
    results = [build.build_wheel(p, tmp_path / name, expected_sha256=sha) for name in ('a', 'b')]
    assert results[0]['wheel_sha256'] == results[1]['wheel_sha256']
    with zipfile.ZipFile(results[0]['wheel']) as z:
        assert z.read('hermes_feishu_card/shared_card_markdown.py') == (ROOT / 'agentcore/card_markdown.py').read_bytes()
        assert z.read('hermes_feishu_card/shared_final_render.py') == (ROOT / 'integrations/hfc/final_render.py').read_bytes()
        assert z.read('hermes_feishu_card/hook_runtime.py') == b'DELIVERY_MUST_NOT_CHANGE = True\n'
        assert z.read(build.SERVER_PATH).decode().count('SUPER_AGENT_SHARED_FINAL_RENDER_V1') == 1
        manifest = json.loads(z.read('hermes_feishu_card/shared_final_render_provenance.json'))
        assert manifest['base_wheel_sha256'] == sha
        rows = csv.reader(io.StringIO(z.read('hermes_feishu_streaming_card-5.0.13.dist-info/RECORD').decode()))
        for name, value, length in rows:
            if not value:
                continue
            data = z.read(name)
            assert int(length) == len(data)
            assert value == 'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('=')


def test_unapproved_input_is_refused_without_output(tmp_path):
    p, _ = base_wheel(tmp_path)
    with pytest.raises(ValueError, match='digest'):
        build.build_wheel(p, tmp_path / 'out', expected_sha256='0' * 64)
    assert not (tmp_path / 'out').exists()


def test_source_drift_is_refused(tmp_path):
    p, sha = base_wheel(tmp_path, server='def drift(): pass\n')
    with pytest.raises(ValueError, match='seam drift'):
        build.build_wheel(p, tmp_path / 'out', expected_sha256=sha)


def test_existing_artifact_is_not_overwritten(tmp_path):
    p, sha = base_wheel(tmp_path)
    result = build.build_wheel(p, tmp_path / 'out', expected_sha256=sha)
    original = Path(result['wheel']).read_bytes()
    with pytest.raises(FileExistsError):
        build.build_wheel(p, tmp_path / 'out', expected_sha256=sha)
    assert Path(result['wheel']).read_bytes() == original


def test_config_selection_is_validated(tmp_path):
    p, sha = base_wheel(tmp_path)
    result = build.build_wheel(p, tmp_path / 'out', expected_sha256=sha)
    with zipfile.ZipFile(result['wheel']) as z:
        namespace = {}
        exec(z.read('hermes_feishu_card/config_rules.py'), namespace)
    normalize = namespace['normalize_card_config']
    value = {'final_renderer': ' Shared '}
    normalize(value, path='card')
    assert value == {'final_renderer': 'shared'}
    for invalid in (True, None, [], 'typo', 1):
        with pytest.raises(ValueError):
            normalize({'final_renderer': invalid}, path='card')
