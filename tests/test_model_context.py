import json

import pytest

from agentcore import model_context as mc


CONFIG = """
model:
  default: main
  provider: litellm
  context_length: 922000
providers:
  litellm:
    base_url: http://127.0.0.1:4000/v1
    default_model: main
auxiliary:
  compression:
    context_length: 1048000
compression:
  threshold_tokens: 900000
  enabled: true
fallback_model:
  - model: main
  - model: backup
coding_instructions:
  - 宁多花 token 也不省验证
"""


def test_config_subset_reads_the_keys_we_need(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text(CONFIG, encoding='utf-8')
    config = mc.load_config_subset(path)

    assert config['model']['default'] == 'main'
    assert config['model']['context_length'] == 922000
    assert config['providers']['litellm']['base_url'] == 'http://127.0.0.1:4000/v1'
    assert config['auxiliary']['compression']['context_length'] == 1048000
    assert config['compression']['threshold_tokens'] == 900000
    # List items and their nested blocks are skipped, not mistaken for maps.
    assert config['fallback_model'] == {}
    assert config['coding_instructions'] == {}


def test_resolve_base_url_prefers_model_then_provider(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text(CONFIG, encoding='utf-8')
    config = mc.load_config_subset(path)
    assert mc.resolve_base_url(config) == 'http://127.0.0.1:4000/v1'

    config['model']['base_url'] = 'http://example.test/v1'
    assert mc.resolve_base_url(config) == 'http://example.test/v1'


def test_parse_models_and_model_info():
    models = mc.parse_models({
        'data': [
            {'id': 'main'},
            {'id': 'flash', 'max_input_tokens': 1048000, 'max_output_tokens': 328000},
        ]
    })
    assert models == {'flash': {'max_input_tokens': 1048000, 'max_output_tokens': 328000}}

    info = mc.parse_model_info({
        'data': [
            {'model_name': 'main', 'litellm_params': {'model_info': {'max_input_tokens': 1048000}}},
            {'model_name': 'main', 'litellm_params': {'model_info': {'max_input_tokens': 1441792}}},
            {'model_name': 'flash', 'litellm_params': {'model_info': {'max_input_tokens': 0}}},
        ]
    })
    # A group advertises the smallest deployment, and invalid values are dropped.
    assert info == {'main': {'max_input_tokens': 1048000}}


def test_merge_prefers_the_standard_list_and_fills_the_gap():
    models = {'flash': {'max_input_tokens': 1048000}}
    info = {'main': {'max_input_tokens': 1048000, 'max_output_tokens': 328000}}
    windows = mc.merge_windows(models, info)

    assert windows['flash'].source == 'models'
    assert windows['main'].source == 'model/info'
    assert windows['main'].max_input_tokens == 1048000
    assert windows['main'].max_output_tokens == 328000


def test_probe_merges_the_admin_endpoint_when_models_omits_the_window():
    seen = []

    def fake_fetch(url, api_key, timeout):
        seen.append(url)
        if url.endswith('/model/info'):
            return {'data': [{'model_name': 'main', 'litellm_params': {
                'model_info': {'max_input_tokens': 1048000, 'max_output_tokens': 328000}}}]}
        return {'data': [{'id': 'main'}]}

    catalog = mc.probe('http://127.0.0.1:4000/v1', fetch=fake_fetch)

    assert seen == [
        'http://127.0.0.1:4000/v1/models',
        'http://127.0.0.1:4000/v1/model/info',
    ]
    assert catalog.effective('main').max_input_tokens == 1048000
    assert catalog.effective('main').source == 'model/info'


def test_probe_survives_one_endpoint_failing():
    def fake_fetch(url, api_key, timeout):
        if url.endswith('/model/info'):
            raise OSError('admin endpoint down')
        return {'data': [{'id': 'main', 'max_input_tokens': 1048000}]}

    catalog = mc.probe('http://127.0.0.1:4000', fetch=fake_fetch)

    assert catalog.effective('main').max_input_tokens == 1048000
    assert any('/v1/model/info' in warning for warning in catalog.warnings)


def test_probe_raises_when_both_endpoints_fail():
    def fake_fetch(url, api_key, timeout):
        raise OSError('down')

    with pytest.raises(mc.ProbeError):
        mc.probe('http://127.0.0.1:4000/v1', fetch=fake_fetch)


def test_check_config_flags_manual_pin_and_mismatched_compression():
    catalog = mc.Catalog(base_url='http://example.test/v1', windows={
        'main': mc.Window('main', max_input_tokens=1048000, source='model/info')})
    warnings = mc.check_config(catalog, {
        'model': {'context_length': 922000},
        'auxiliary': {'compression': {'context_length': 1048000}},
        'compression': {'threshold_tokens': 900000},
    }, 'main')

    assert any('model.context_length=922000' in warning for warning in warnings)
    assert not any('auxiliary.compression' in warning for warning in warnings)
    assert not any('threshold_tokens' in warning for warning in warnings)


def test_check_config_flags_a_threshold_above_the_window():
    catalog = mc.Catalog(base_url='http://example.test/v1', windows={
        'main': mc.Window('main', max_input_tokens=100_000, source='models')})
    warnings = mc.check_config(catalog, {
        'compression': {'threshold_tokens': 200_000},
    }, 'main')

    assert any('threshold_tokens' in warning for warning in warnings)


def test_cli_reports_the_effective_window(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'config.yaml'
    path.write_text(CONFIG, encoding='utf-8')
    monkeypatch.setattr(mc, 'probe', lambda *a, **k: mc.Catalog(
        base_url='http://example.test/v1',
        windows={'main': mc.Window('main', max_input_tokens=1048000, source='model/info')}))

    code = mc.main(['--config', str(path), '--json'])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload['context_length'] == 1048000
    assert payload['source'] == 'model/info'
    assert any('model.context_length=922000' in warning for warning in payload['warnings'])


def test_cli_returns_one_when_the_model_has_no_window(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'config.yaml'
    path.write_text(CONFIG, encoding='utf-8')
    monkeypatch.setattr(mc, 'probe', lambda *a, **k: mc.Catalog(
        base_url='http://example.test/v1', windows={}))

    code = mc.main(['--config', str(path), '--json'])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload['context_length'] is None


def test_performance_budget_is_independent_of_catalog_window():
    config = {'compression': {'threshold_tokens': 900000}}
    warnings = mc.check_compression_budget(config, 200000)
    assert len(warnings) == 1 and '900000' in warnings[0] and '200000' in warnings[0]
    assert mc.check_compression_budget(config, 900000) == []
    assert mc.check_compression_budget({}, 200000) == []
    assert 'disabled' in mc.check_compression_budget({'compression': {'enabled': False}}, 200000)[0]


def test_performance_budget_rejects_non_positive_and_boolean():
    import pytest
    for value in (0, -1, True):
        with pytest.raises(ValueError):
            mc.check_compression_budget({}, value)


def test_sliding_context_compaction_approaching_threshold():
    # 1. Under threshold: no compaction
    messages = [
        {"role": "system", "content": "You are a software engineer."},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    compacted = mc.compact_context_window(messages, max_tokens=10000, threshold_tokens=8000)
    assert len(compacted) == 3
    assert compacted == messages

    # 2. Over threshold: preserves system instruction, tail turns, compacts middle turns
    long_history = [
        {"role": "system", "content": "You are a software engineer. " * 10},
        {"role": "user", "content": "Turn 1 request for module A " * 10},
        {"role": "assistant", "content": "Turn 1 response implemented module A " * 10},
        {"role": "user", "content": "Turn 2 request for module B " * 10},
        {"role": "assistant", "content": "Turn 2 response implemented module B " * 10},
        {"role": "user", "content": "Turn 3 request for module C " * 10},
        {"role": "assistant", "content": "Turn 3 response implemented module C " * 10},
        {"role": "user", "content": "Turn 4 latest question: can we refactor module C?"},
        {"role": "assistant", "content": "Sure, here is the plan."},
    ]
    # Total tokens is high; set small threshold
    result = mc.compact_context_window(
        long_history,
        max_tokens=500,
        threshold_tokens=250,
        keep_recent_turns=1,
    )
    # Head system preserved
    assert result[0]["role"] == "system"
    assert "software engineer" in result[0]["content"]

    # Compaction summary message created in middle
    assert any("[Context Sliding Compaction:" in m.get("content", "") for m in result)

    # Tail recent turns preserved
    assert result[-1]["content"] == "Sure, here is the plan."
    assert result[-2]["content"] == "Turn 4 latest question: can we refactor module C?"

    # Budget boundary validation: non-positive tokens rejected
    with pytest.raises(ValueError, match="positive integer"):
        mc.compact_context_window(messages, max_tokens=0)

    with pytest.raises(ValueError, match="positive integer"):
        mc.compact_context_window(messages, max_tokens=1000, threshold_tokens=-1)

