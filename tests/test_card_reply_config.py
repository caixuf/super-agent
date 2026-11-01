"""A card flag alone cannot enable the stream-preview card dispatch path."""
import importlib.util
from pathlib import Path

import pytest
import yaml

PATCH = Path(__file__).resolve().parents[1] / 'patches/apply_feishu_card_replies.py'


@pytest.mark.parametrize('streaming,override,expected', [
    ({}, None, 1),
    ({'enabled': False}, None, 1),
    ({'enabled': 'false'}, None, 1),
    ({'enabled': True}, None, 0),
    ({'enabled': True, 'transport': 'off'}, None, 1),
    ({'enabled': True}, False, 1),
    ({'enabled': True}, 'false', 1),
    ({'mode': 'edit'}, None, 0),
    ({'transport': 'edit'}, None, 1),
    ({'mode': False}, None, 1),
    ({'enabled': True, 'transport': None, 'mode': 'off'}, None, 1),
])
def test_card_config_requires_effective_streaming(tmp_path, monkeypatch, capsys,
                                                 streaming, override, expected):
    spec = importlib.util.spec_from_file_location('_card_config_check', PATCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = {'platforms': {'feishu': {'card_replies': True, 'exec_approval_text': True}},
              'streaming': streaming,
              'display': {'platforms': {'feishu': {'streaming': override}}}}
    (tmp_path / 'config.yaml').write_text(yaml.safe_dump(config))
    monkeypatch.setenv('HERMES_PROFILE_DIR', str(tmp_path))
    assert module.config_check() == expected
    output = capsys.readouterr().out
    if expected:
        assert 'streaming' in output and '[FAIL]' in output
