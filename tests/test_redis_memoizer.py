"""RedisMemoizer: shared cache + cross-instance single-flight dedup via Redis.

Uses fakeredis (a single FakeServer shared by multiple client instances,
simulating multiple ECS instances talking to one ElastiCache endpoint).
Concurrency is exercised with real threads.

Note: redis-py's built-in Lock releases via an EVALSHA'd Lua script, which
fakeredis does not implement — that's why RedisMemoizer hand-rolls the lock
with SET NX + WATCH/MULTI/EXEC instead (see memoizer.py's RedisMemoizer docstring).
"""

import threading
import time

import fakeredis
import pytest

from app.services.caching.memoizer import RedisMemoizer


@pytest.fixture
def server():
    return fakeredis.FakeServer()


def make_memoizer(server, **kwargs):
    client = fakeredis.FakeStrictRedis(server=server)
    defaults = {"namespace": "t", "ttl_seconds": 60, "lock_ttl_seconds": 5, "wait_timeout": 3}
    defaults.update(kwargs)
    return RedisMemoizer(client, **defaults)


def test_cache_miss_computes_and_stores(server):
    m = make_memoizer(server)
    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        return "computed"

    assert m.get_or_compute("k", factory) == "computed"
    assert calls == 1
    assert m.redis.exists(m._cache_key("k"))


def test_cache_hit_skips_factory(server):
    m = make_memoizer(server)
    m.get_or_compute("k", lambda: "first")
    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        return "second"

    assert m.get_or_compute("k", factory) == "first"
    assert calls == 0


def test_second_instance_sees_first_instances_cached_value(server):
    """Simulates cross-instance sharing: two RedisMemoizers on one fake Redis server."""
    writer = make_memoizer(server)
    reader = make_memoizer(server)

    writer.get_or_compute("k", lambda: "shared")

    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        return "should not run"

    assert reader.get_or_compute("k", factory) == "shared"
    assert calls == 0


def test_concurrent_instances_share_single_compute(server):
    """Two RedisMemoizers (simulating two ECS instances) racing on the same key:
    one computes, the other waits on pub/sub and gets the same result."""
    m1 = make_memoizer(server)
    m2 = make_memoizer(server)
    started = threading.Barrier(2)
    proceed = threading.Event()
    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        proceed.wait(timeout=3)
        return "computed"

    results: list[str] = []

    def worker(m):
        started.wait()
        results.append(m.get_or_compute("key", factory))

    t1 = threading.Thread(target=worker, args=(m1,))
    t2 = threading.Thread(target=worker, args=(m2,))
    t1.start()
    t2.start()
    time.sleep(0.2)  # let both threads past the barrier and into lock/subscribe
    proceed.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert results == ["computed", "computed"]
    assert calls == 1


def test_holder_crash_lets_waiter_take_over(server):
    """If a lock-holder dies before publishing, a waiter should not hang forever —
    once the lock's TTL expires it takes over and computes the value itself."""
    m1 = make_memoizer(server, lock_ttl_seconds=1, wait_timeout=3)
    m2 = make_memoizer(server, lock_ttl_seconds=1, wait_timeout=3)

    # Simulate m1 acquiring the lock and crashing: never calls factory or releases.
    import uuid

    m1.redis.set(m1._lock_key("key"), uuid.uuid4().hex.encode(), nx=True, ex=1)

    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        return "recovered"

    result = m2.get_or_compute("key", factory)
    assert result == "recovered"
    assert calls == 1


def test_exception_in_factory_releases_lock_and_does_not_cache(server):
    m = make_memoizer(server)
    calls = 0

    def failing():
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        m.get_or_compute("k", failing)

    assert not m.redis.exists(m._cache_key("k"))
    assert m.redis.exists(m._lock_key("k")) == 0  # lock released, not left dangling

    # A later call must not be poisoned by the earlier failure.
    assert m.get_or_compute("k", lambda: "ok") == "ok"
    assert calls == 1
