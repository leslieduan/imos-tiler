"""Deduper: in-flight dedup only, no caching. Threading is the entire point,
so concurrency cases are exercised with real threads + a barrier, not mocked.
"""

import threading
import time

import pytest

from app.services.caching.deduper import Deduper


def test_sequential_calls_each_recompute():
    """No caching: back-to-back calls for the same key both run the factory."""
    d = Deduper()
    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        return calls

    assert d.dedupe("k", factory) == 1
    assert d.dedupe("k", factory) == 2


def test_concurrent_calls_share_single_compute():
    """Two threads racing on the same key both get the first thread's result and
    only one factory invocation happens."""
    d = Deduper()
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
        results.append(d.dedupe("key", factory))

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    # Let both threads enter dedupe; one of them will start the compute,
    # the other will see the in-flight Future and block on it.
    time.sleep(0.05)
    proceed.set()
    t1.join(timeout=2)
    t2.join(timeout=2)

    assert results == ["computed", "computed"]
    assert calls == 1


def test_exception_in_factory_propagates_and_clears_inflight():
    """A failed compute must NOT poison subsequent calls for the same key."""
    d = Deduper()
    calls = 0

    def failing():
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        d.dedupe("k", failing)
    # Second attempt should run again, not surface the cached future from the first.
    with pytest.raises(RuntimeError, match="boom"):
        d.dedupe("k", failing)
    assert calls == 2
    # And a successful subsequent attempt should work cleanly.
    assert d.dedupe("k", lambda: "ok") == "ok"


def test_exception_propagates_to_concurrent_waiter():
    """A second thread waiting on an in-flight Future sees the same exception."""
    d = Deduper()
    proceed = threading.Event()

    def failing():
        proceed.wait(timeout=2)
        raise ValueError("from worker")

    results: list = []

    def worker():
        try:
            d.dedupe("k", failing)
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


def test_different_keys_do_not_share_compute():
    d = Deduper()
    calls = []

    def factory(k):
        calls.append(k)
        return k

    assert d.dedupe("a", lambda: factory("a")) == "a"
    assert d.dedupe("b", lambda: factory("b")) == "b"
    assert calls == ["a", "b"]
