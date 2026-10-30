import importlib.util
from pathlib import Path
import sys
import types
import threading
import pytest


def load(monkeypatch):
    provider = types.ModuleType('agent.memory_provider')
    provider.MemoryProvider = object
    provider.RecallStatus = object
    provider.is_trivial_prompt = lambda query: False
    provider.spawn_context_thread = lambda target, name, args: threading.Thread(target=target, name=name, args=args, daemon=True)
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


def test_prefetch_waits_for_existing_background_recall_without_duplicate(monkeypatch):
    module = load(monkeypatch)
    monkeypatch.setattr(module, 'spawn_context_thread',
                        lambda target, name, args: threading.Thread(target=target, name=name, args=args))
    monkeypatch.setattr(module, 'RecallStatus', lambda **kwargs: kwargs)
    provider = module.LocalVecProvider()
    provider._cmd = ['configured']; provider._timeout = 2
    entered = threading.Event(); release = threading.Event(); calls = []
    def recall(query, key):
        calls.append(query); entered.set(); release.wait(2)
        with provider._lock: provider._cache[key] = ('found', 1)
    monkeypatch.setattr(provider, '_recall', recall)
    provider.queue_prefetch('query')
    assert entered.wait(1)
    provider.queue_prefetch('query')
    result = []
    thread = threading.Thread(target=lambda: result.append(provider.prefetch('query')))
    thread.start(); release.set(); thread.join(3)
    assert not thread.is_alive()
    assert calls == ['query'] and result == ['found']


def test_prefetch_timeout_does_not_start_a_second_recall(monkeypatch):
    module = load(monkeypatch)
    monkeypatch.setattr(module, 'spawn_context_thread',
                        lambda target, name, args: threading.Thread(target=target, name=name, args=args))
    provider = module.LocalVecProvider(); provider._cmd = ['configured']; provider._timeout = .1
    entered = threading.Event(); release = threading.Event(); done = threading.Event(); calls = []
    def recall(query, key):
        calls.append(query); entered.set(); release.wait(2); done.set()
    monkeypatch.setattr(provider, '_recall', recall)
    try:
        provider.queue_prefetch('query')
        assert entered.wait(1)
        assert provider.prefetch('query') == module.RECALL_UNAVAILABLE
        assert calls == ['query']
    finally:
        release.set(); assert done.wait(2)


def test_foreground_without_prefetch_returns_warning_before_slow_worker_finishes(monkeypatch):
    module = load(monkeypatch)
    monkeypatch.setattr(module, 'MAX_FOREGROUND_WAIT_S', .1)
    provider = module.LocalVecProvider(); provider._cmd = ['configured']; provider._timeout = 2
    entered = threading.Event(); release = threading.Event(); done = threading.Event()
    def recall(query, key):
        entered.set(); release.wait(2); done.set(); return True
    monkeypatch.setattr(provider, '_recall', recall)
    try:
        assert provider.prefetch('query') == module.RECALL_UNAVAILABLE
        assert entered.is_set() and not done.is_set()
    finally:
        release.set(); assert done.wait(2)


def test_concurrent_waiters_each_receive_the_shared_result(monkeypatch):
    module = load(monkeypatch)
    monkeypatch.setattr(module, 'spawn_context_thread',
                        lambda target, name, args: threading.Thread(target=target, name=name, args=args))
    monkeypatch.setattr(module, 'RecallStatus', lambda **kwargs: kwargs)
    provider = module.LocalVecProvider(); provider._cmd = ['configured']; provider._timeout = 2
    entered = threading.Event(); release = threading.Event(); calls = []
    def recall(query, key):
        calls.append(query); entered.set(); release.wait(2)
        with provider._lock: provider._cache[key] = ('found', 1)
    monkeypatch.setattr(provider, '_recall', recall)
    provider.queue_prefetch('query'); assert entered.wait(1)
    results = []
    a = threading.Thread(target=lambda: results.append(provider.prefetch('query')))
    b = threading.Thread(target=lambda: results.append(provider.prefetch('query')))
    a.start(); b.start()
    # Both waiters must already be waiting before the background operation ends.
    for _ in range(100):
        if a.is_alive() and b.is_alive(): break
        threading.Event().wait(.01)
    release.set(); a.join(3); b.join(3)
    assert not a.is_alive() and not b.is_alive()
    assert calls == ['query'] and sorted(results) == ['found', 'found']


def test_overload_does_not_start_unbounded_recalls(monkeypatch, caplog):
    module = load(monkeypatch)
    provider = module.LocalVecProvider(); provider._cmd = ['configured']
    provider._inflight = {str(i): threading.Event() for i in range(module.MAX_INFLIGHT_RECALLS)}
    monkeypatch.setattr(provider, '_recall', lambda query, key: pytest.fail('unexpected recall'))
    provider.queue_prefetch('new query')
    assert provider.prefetch('new query') == module.RECALL_UNAVAILABLE
    assert len(provider._inflight) == module.MAX_INFLIGHT_RECALLS
    assert 'recall capacity reached' in caplog.text


def test_failed_recall_is_distinct_from_a_valid_empty_result(monkeypatch):
    module = load(monkeypatch)
    provider = module.LocalVecProvider(); provider._cmd = ['configured']; provider._cfg = {}
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: types.SimpleNamespace(returncode=2, stdout=''))
    assert provider.prefetch('first') == module.RECALL_UNAVAILABLE
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: types.SimpleNamespace(returncode=0, stdout='{"results": []}'))
    assert provider.prefetch('second') == ''
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: types.SimpleNamespace(returncode=0, stdout='{"ok": false, "results": []}'))
    assert provider.prefetch('third') == module.RECALL_UNAVAILABLE


def test_cache_is_not_consumed_before_result_is_published(monkeypatch):
    module = load(monkeypatch)
    monkeypatch.setattr(module, 'spawn_context_thread',
                        lambda target, name, args: threading.Thread(target=target, name=name, args=args))
    monkeypatch.setattr(module, 'RecallStatus', lambda **kwargs: kwargs)
    provider = module.LocalVecProvider(); provider._cmd = ['configured']; provider._timeout = 2
    written = threading.Event(); release = threading.Event(); calls = []
    def recall(query, key):
        calls.append(query)
        with provider._lock: provider._cache[key] = ('found', 1)
        written.set(); release.wait(2)
    monkeypatch.setattr(provider, '_recall', recall)
    provider.queue_prefetch('query'); assert written.wait(1)
    event = provider._inflight[('', 'query')]; original_wait = event.wait
    both_waiting = threading.Event(); waiter_lock = threading.Lock(); waiter_count = []
    def observed_wait(timeout=None):
        with waiter_lock:
            waiter_count.append(1)
            if len(waiter_count) == 2: both_waiting.set()
        return original_wait(timeout)
    monkeypatch.setattr(event, 'wait', observed_wait)
    results = []
    a = threading.Thread(target=lambda: results.append(provider.prefetch('query')))
    b = threading.Thread(target=lambda: results.append(provider.prefetch('query')))
    a.start(); b.start(); assert both_waiting.wait(1)
    release.set(); a.join(3); b.join(3)
    assert not a.is_alive() and not b.is_alive()
    assert calls == ['query'] and sorted(results) == ['found', 'found']


def test_recall_cache_is_scoped_by_session(monkeypatch):
    module = load(monkeypatch)
    monkeypatch.setattr(module, 'RecallStatus', lambda **kwargs: kwargs)
    provider = module.LocalVecProvider(); provider._cmd = ['configured']
    calls = []
    def recall(query, key):
        calls.append(key)
        with provider._lock: provider._cache[key] = ('for-' + key[0], 1)
        return True
    monkeypatch.setattr(provider, '_recall', recall)
    provider._cache[('session-a', 'same')] = ('cached-a', 1)
    assert provider.prefetch('same', session_id='session-b') == 'for-session-b'
    assert provider.prefetch('same', session_id='session-a') == 'cached-a'
    assert calls == [('session-b', 'same')]
