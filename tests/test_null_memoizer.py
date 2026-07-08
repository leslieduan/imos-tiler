from app.utils.memoizer import CacheBackend, NullMemoizer


def test_null_memoizer_always_recomputes():
    m = NullMemoizer()
    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        return calls

    assert m.get_or_compute("k", factory) == 1
    assert m.get_or_compute("k", factory) == 2


def test_null_memoizer_contains_always_false():
    m = NullMemoizer()
    m.get_or_compute("k", lambda: "v")
    assert m.contains("k") is False


def test_null_memoizer_is_a_cache_backend():
    assert isinstance(NullMemoizer(), CacheBackend)
