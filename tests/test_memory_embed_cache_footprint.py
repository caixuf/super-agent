"""嵌入缓存的常驻内存必须有上限，且不得靠「整表清空」实现。

为什么单独立一组测试：缓存值从 Python float list 改成 packed float32 字节后，
「上限」和「逐出」的语义都变了 —— 这两件事如果只靠肉眼 review，
下一个人很容易把 clear() 换回来，内存占用又悄悄涨回 ~150MB。
"""
from agentcore.memory import memory_store
from agentcore.memory.config import get_config


def test_cache_stores_packed_bytes_not_float_lists(engine):
    text = 'packed-cache-footprint-probe'
    memory_store._embed_with_cache([text])
    stored = memory_store._EMBED_CACHE.get(text)
    assert isinstance(stored, bytes)
    assert len(stored) == get_config().embed_dim * 4


def test_cached_vector_survives_round_trip(engine, monkeypatch):
    dim = get_config().embed_dim
    monkeypatch.setattr(get_config(), 'embed_fn', lambda texts: [[0.5] * dim for _ in texts])
    first = memory_store._embed_with_cache(['round-trip-probe'])
    second = memory_store._embed_with_cache(['round-trip-probe'])
    assert first == second == [[0.5] * dim]


def test_cache_evicts_oldest_instead_of_clearing_everything(engine, monkeypatch):
    dim = get_config().embed_dim
    monkeypatch.setattr(get_config(), 'embed_fn', lambda texts: [[0.25] * dim for _ in texts])
    original_max = memory_store._EMBED_CACHE_MAX
    memory_store._EMBED_CACHE_MAX = 2
    try:
        memory_store._EMBED_CACHE.clear()
        for index in range(4):
            memory_store._embed_with_cache(['evict-probe-%d' % index])
        # 超限后逐出最旧的：最早两条不在，最新两条仍在。
        assert 'evict-probe-0' not in memory_store._EMBED_CACHE
        assert 'evict-probe-1' not in memory_store._EMBED_CACHE
        assert 'evict-probe-2' in memory_store._EMBED_CACHE
        assert 'evict-probe-3' in memory_store._EMBED_CACHE
    finally:
        memory_store._EMBED_CACHE_MAX = original_max
        memory_store._EMBED_CACHE.clear()
