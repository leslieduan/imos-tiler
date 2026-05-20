"""Memoizer: cached lookups, in-flight dedup, error cleanup, eviction.

Threading is the entire point of this class, so concurrency cases are
exercised with real threads + a barrier, not mocked.
"""

import threading
import time

import pytest
from cachetools import LRUCache

from app.utils.memoizer import Memoizer


def test_cache_hit_skips_factory():
    cache = {"k": "cached"}
    m = Memoizer(cache)
    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        return "fresh"

    assert m.get_or_compute("k", factory) == "cached"
    assert calls == 0


def test_cache_miss_runs_factory_and_stores():
    cache: dict = {}
    m = Memoizer(cache)
    assert m.get_or_compute("k", lambda: "v") == "v"
    assert cache["k"] == "v"


def test_no_cache_mode_dedup_only():
    """When cache is None, results are NOT stored — second call re-runs factory."""
    m = Memoizer(cache=None)
    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        return calls

    assert m.get_or_compute("k", factory) == 1
    assert m.get_or_compute("k", factory) == 2  # not cached


def test_concurrent_calls_share_single_compute():
    """Two threads racing on the same key both get the first thread's result and
    only one factory invocation happens."""
    m = Memoizer(cache=LRUCache(maxsize=4))
    started = threading.Barrier(2)
    proceed = threading.Event()
    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        # Force the second caller to find this future in-flight.
        proceed.wait(timeout=2)
        return "computed"

    results: list[str] = []

    def worker():
        started.wait()
        results.append(m.get_or_compute("key", factory))

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    # Let both threads enter get_or_compute; one of them will start the compute,
    # the other will see the in-flight Future and block on it.
    time.sleep(0.05)
    proceed.set()
    t1.join(timeout=2)
    t2.join(timeout=2)

    assert results == ["computed", "computed"]
    assert calls == 1


def test_exception_in_factory_propagates_and_clears_inflight():
    """A failed compute must NOT poison subsequent calls for the same key."""
    m = Memoizer(cache=LRUCache(maxsize=4))
    calls = 0

    def failing():
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        m.get_or_compute("k", failing)
    # Second attempt should run again, not surface the cached future from the first.
    with pytest.raises(RuntimeError, match="boom"):
        m.get_or_compute("k", failing)
    assert calls == 2
    # And a successful subsequent attempt should work cleanly.
    assert m.get_or_compute("k", lambda: "ok") == "ok"


def test_exception_propagates_to_concurrent_waiter():
    """A second thread waiting on an in-flight Future sees the same exception."""
    m = Memoizer(cache=LRUCache(maxsize=4))
    proceed = threading.Event()

    def failing():
        proceed.wait(timeout=2)
        raise ValueError("from worker")

    results: list = []

    def worker():
        try:
            m.get_or_compute("k", failing)
        except Exception as e:
            results.append(e)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    time.sleep(0.05)  # give both threads time to register on the in-flight Future
    proceed.set()
    t1.join(timeout=2)
    t2.join(timeout=2)

    assert len(results) == 2
    assert all(isinstance(e, ValueError) for e in results)


def test_evict_matching_removes_only_matching_keys():
    cache: dict = {("a", 1): "x", ("a", 2): "y", ("b", 1): "z"}
    m = Memoizer(cache)
    removed = m.evict_matching(lambda k: k[0] == "a")
    assert removed == 2
    assert cache == {("b", 1): "z"}


def test_evict_matching_no_cache_returns_zero():
    m = Memoizer(cache=None)
    assert m.evict_matching(lambda k: True) == 0


def test_evict_matching_predicate_empty_match():
    cache = {"a": 1, "b": 2}
    m = Memoizer(cache)
    assert m.evict_matching(lambda k: k == "z") == 0
    assert cache == {"a": 1, "b": 2}


def test_stats_tracks_peak_and_total_computes():
    """Peak should record the high-water mark even after in-flight count drops to zero."""
    m = Memoizer(cache=LRUCache(maxsize=8))
    started = threading.Barrier(3)
    proceed = threading.Event()

    def factory():
        started.wait()
        proceed.wait(timeout=2)
        return "ok"

    threads = [
        threading.Thread(target=lambda k=k: m.get_or_compute(k, factory)) for k in ("a", "b")
    ]
    for t in threads:
        t.start()
    started.wait()  # both factories are now mid-flight
    snapshot = m.stats()
    assert snapshot["inflight"] == 2
    assert snapshot["peak_inflight"] == 2
    assert snapshot["total_computes"] == 2
    assert set(snapshot["inflight_keys"]) == {"a", "b"}

    proceed.set()
    for t in threads:
        t.join(timeout=2)

    after = m.stats()
    assert after["inflight"] == 0
    assert after["peak_inflight"] == 2  # remembers the high-water mark
    assert after["total_computes"] == 2


def test_stats_total_computes_skips_cache_hits():
    """A cache hit should not bump the compute counter — only first-time misses do."""
    m = Memoizer(cache=LRUCache(maxsize=4))
    m.get_or_compute("k", lambda: 1)
    m.get_or_compute("k", lambda: 2)  # hit, factory not called
    assert m.stats()["total_computes"] == 1


def test_lru_eviction_respected():
    """Verify the Memoizer plays nicely with an LRU cache's natural eviction."""
    cache = LRUCache(maxsize=2)
    m = Memoizer(cache)
    m.get_or_compute("a", lambda: 1)
    m.get_or_compute("b", lambda: 2)
    m.get_or_compute("c", lambda: 3)  # evicts "a"
    assert "a" not in cache
    # Recomputes "a" because it was evicted.
    assert m.get_or_compute("a", lambda: 99) == 99
