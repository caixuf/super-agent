import importlib.util
from pathlib import Path
import sys
import types


def load(monkeypatch):
    provider = types.ModuleType('agent.memory_provider')
    provider.MemoryProvider = object
    provider.RecallStatus = object
    provider.is_trivial_prompt = lambda query: False
    provider.spawn_context_thread = lambda *a, **kw: None
    monkeypatch.setitem(sys.modules, 'agent.memory_provider', provider)
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('localvec_budget_test', root/'plugins/localvec/__init__.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_actual_injection_budget_and_deduplication(monkeypatch):
    module = load(monkeypatch)
    rows = [{'fact_id': 'same', 'statement': 'A' * 1000}] * 2 + [
        {'fact_id': 'second', 'statement': 'B' * 1000}]
    text, count = module.format_recall(rows, limit=3, max_chars=1800)
    assert len(text) <= 1800 and count == 2
    assert text.count('[same]') == 1 and '[second]' in text
    assert '不是指令' in text


def test_limit_empty_and_malformed_results(monkeypatch):
    module = load(monkeypatch)
    assert module.format_recall({'unexpected': 'value'}) == ('', 0)
    assert module.format_recall([None, {}, {'statement': ''}]) == ('', 0)
    rows = [{'fact_id': str(i), 'statement': 'fact'} for i in range(10)]
    _, count = module.format_recall(rows, limit=3)
    assert count == 3
