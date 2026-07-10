from maps_mcp.cache import ttl_cache


def test_ttl_cache_returns_cached_value_for_repeated_args():
    calls = []

    @ttl_cache(ttl_seconds=60)
    def f(x):
        calls.append(x)
        return x * 2

    assert f(3) == 6
    assert f(3) == 6
    assert calls == [3]


def test_ttl_cache_distinguishes_args():
    @ttl_cache(ttl_seconds=60)
    def f(x):
        return x * 2

    assert f(3) == 6
    assert f(4) == 8


def test_ttl_cache_handles_list_args():
    calls = []

    @ttl_cache(ttl_seconds=60)
    def f(items):
        calls.append(items)
        return list(items)

    assert f(["a", "b"]) == ["a", "b"]
    assert f(["a", "b"]) == ["a", "b"]
    assert len(calls) == 1
