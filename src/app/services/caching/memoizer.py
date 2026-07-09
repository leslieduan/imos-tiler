"""Cross-request dedup + caching for the L1/L2 cache stack.

``CacheBackend`` is the shared contract; ``NullMemoizer`` and ``RedisMemoizer``
are the two selectable implementations (see ``backend_factory.create_memoizer``,
chosen via ``CACHE_BACKEND``). There is deliberately no in-process caching
backend here — that keeps every ECS instance's cache state identical (either
none, or shared via Redis) instead of N private copies.

In-process dedup-only coalescing (``services.caching.deduper.Deduper``) is a
separate, simpler concern that doesn't fit this module's cache-or-recompute
contract — it never stores anything. Each ``Deduper`` instance lives with its
one consumer (e.g. ``rendering.data_tiles._processed_dedup``,
``store.slice_loader._slice_dedup``), not paired with a ``CacheBackend`` here.

NOT a replacement for ``services.store.registry.StoreRegistry`` — that adds TTL +
stale-while-revalidate + background refresh on top of the dedup pattern, which
this module deliberately does not model.
"""

import pickle
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Hashable
from typing import TypeVar, cast

import redis
import redis.exceptions

T = TypeVar("T")


class CacheBackend(ABC):
    """Shared contract for cache + cross-instance dedup implementations.

    ``get_or_compute`` is the only method any production caller invokes.
    """

    @abstractmethod
    def get_or_compute(self, key: Hashable, factory: Callable[[], T]) -> T:
        """Return cached value, wait on an in-flight compute, or run ``factory()`` once."""


class NullMemoizer(CacheBackend):
    """No caching, no dedup — every call runs ``factory()``. Explicit opt-out
    backend for ``CACHE_BACKEND=none``; a stampede of concurrent identical
    requests will all recompute, which is the accepted cost of disabling
    caching entirely."""

    def get_or_compute(self, key: Hashable, factory: Callable[[], T]) -> T:
        return factory()


class RedisMemoizer(CacheBackend):
    """Redis-backed cache + cross-instance single-flight dedup.

    Implements the same ``CacheBackend`` contract as ``NullMemoizer``, but the
    cache and the in-flight coordination both live in Redis so every ECS
    instance shares them. In-process dedup helpers coordinate via a
    ``threading``/``concurrent.futures.Future``, which only works within one
    process — cross-instance single-flight needs a real distributed lock plus
    a pub/sub wakeup so waiters aren't left with no way to find out when the
    holder finishes.

    The lock is a hand-rolled SET-NX + WATCH/MULTI/EXEC release rather than
    redis-py's built-in ``Lock`` (which releases via an EVALSHA'd Lua script):
    WATCH/MULTI/EXEC gives the same "only delete if I still own it" guarantee
    without requiring server-side scripting support.
    """

    def __init__(
        self,
        client: redis.Redis,
        namespace: str,
        ttl_seconds: int,
        lock_ttl_seconds: int = 30,
        wait_timeout: float = 15.0,
    ) -> None:
        self.redis = client
        self.namespace = namespace
        self.ttl_seconds = ttl_seconds
        self.lock_ttl_seconds = lock_ttl_seconds
        self.wait_timeout = wait_timeout

    def _cache_key(self, key: Hashable) -> str:
        return f"{self.namespace}:cache:{key!r}"

    def _lock_key(self, key: Hashable) -> str:
        return f"{self.namespace}:lock:{key!r}"

    def _channel(self, key: Hashable) -> str:
        return f"{self.namespace}:done:{key!r}"

    def get_or_compute(self, key: Hashable, factory: Callable[[], T]) -> T:
        cache_key = self._cache_key(key)

        while True:
            cached = self.redis.get(cache_key)
            if cached is not None:
                return cast(T, pickle.loads(cast(bytes, cached)))

            lock_key = self._lock_key(key)
            token = uuid.uuid4().hex.encode()
            if self.redis.set(lock_key, token, nx=True, ex=self.lock_ttl_seconds):
                try:
                    result = factory()
                    self.redis.set(cache_key, pickle.dumps(result), ex=self.ttl_seconds)
                    self.redis.publish(self._channel(key), "ok")
                    return result
                except Exception:
                    self.redis.publish(self._channel(key), "error")
                    raise
                finally:
                    self._release_lock(lock_key, token)

            if not self._wait_for_holder(key):
                continue  # holder vanished without a result — try to become the holder ourselves

    def _release_lock(self, lock_key: str, token: bytes) -> None:
        """Delete the lock only if we still own it (WATCH aborts the transaction
        if another instance already changed it — e.g. our TTL expired and a new
        holder took over — so we never delete someone else's lock)."""
        with self.redis.pipeline() as pipe:
            try:
                pipe.watch(lock_key)
                if pipe.get(lock_key) != token:
                    pipe.unwatch()
                    return
                pipe.multi()
                pipe.delete(lock_key)
                pipe.execute()
            except redis.exceptions.WatchError:
                pass

    def _wait_for_holder(self, key: Hashable) -> bool:
        """Subscribe-then-recheck to avoid missing a notification published between
        our failed lock-acquire and the subscribe call. Returns True if the caller
        should re-check the cache (a result likely landed); False if the holder
        appears to have vanished and the caller should retry the whole operation."""
        pubsub = self.redis.pubsub()
        try:
            pubsub.subscribe(self._channel(key))  # blocks until the server acks the subscribe

            cached = self.redis.get(self._cache_key(key))
            if cached is not None:
                return True

            message = pubsub.get_message(timeout=self.wait_timeout, ignore_subscribe_messages=True)
            if message is not None and message.get("type") == "message":
                return True

            # Timed out waiting, or the holder died before publishing. If the lock
            # is gone, the holder is done (successfully or not) or crashed — either
            # way, fall through so the caller retries from the top.
            return False
        finally:
            pubsub.close()
