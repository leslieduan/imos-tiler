"""Concurrent-request dedup + optional caching.

Two patterns recur in this project:
  (a) cache + dedup — concurrent callers requesting the same key share a single
      computation, and the result is stored in an LRU for later reuse
      (services.caching.slice_cache.load_slice, services.rendering.data_tiles._get_processed).
  (b) dedup only — there's no cache (e.g. results are too large or already cached
      downstream), but identical in-flight requests should still share work
      (routers.visual_tiles tile/bbox dedup).

Both shapes used to be inlined as the same ~25-line "check cache → create Future
→ fast-path wait → compute → publish → cleanup" boilerplate. This class is that
boilerplate, once.

Errors propagate to all concurrent waiters via the shared Future, and the
in-flight entry is removed in ``finally`` so a failed compute does not
permanently block subsequent attempts for the same key.

NOT a replacement for ``services.caching.slice_cache._get_store`` — that adds TTL +
stale-while-revalidate + background refresh on top of the dedup pattern, which
this helper deliberately does not model.
"""

import concurrent.futures
import pickle
import threading
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Hashable, MutableMapping
from typing import TypeVar, cast

import redis
import redis.exceptions

T = TypeVar("T")


class CacheBackend(ABC):
    """Shared contract for cache + cross-instance dedup implementations.

    Only ``get_or_compute`` and ``contains`` are part of this interface —
    they're the only methods any production caller invokes. ``Memoizer``'s
    ``stats()``/``evict_matching()`` are debug/introspection extras specific
    to the in-memory implementation and are not required of other backends.
    """

    @abstractmethod
    def get_or_compute(self, key: Hashable, factory: Callable[[], T]) -> T:
        """Return cached value, wait on an in-flight compute, or run ``factory()`` once."""

    @abstractmethod
    def contains(self, key: Hashable) -> bool:
        """Peek whether ``key`` is cached, without triggering a compute."""


class Memoizer(CacheBackend):
    def __init__(self, cache: MutableMapping | None = None) -> None:
        """`cache` is any MutableMapping (dict, cachetools.LRUCache, …) or None for dedup-only."""
        self.cache = cache
        self._lock = threading.Lock()
        self._inflight: dict[Hashable, concurrent.futures.Future] = {}
        self._peak_inflight = 0
        self._total_computes = 0

    def get_or_compute(self, key: Hashable, factory: Callable[[], T]) -> T:
        """Return cached value, wait on an in-flight compute, or run ``factory()`` once.

        Must be called from a worker thread (sync ``def`` handler or
        ``anyio.to_thread.run_sync``), never directly from an ``async def``
        coroutine on the event loop. Waiters block on ``future.result()``,
        which is a blocking syscall — invoking this from the loop would
        freeze every other request behind a single in-flight compute.
        """
        should_compute = False
        with self._lock:
            if self.cache is not None and key in self.cache:
                return self.cache[key]  # type: ignore[no-any-return]
            if key in self._inflight:
                future = self._inflight[key]
            else:
                future = concurrent.futures.Future()
                self._inflight[key] = future
                self._total_computes += 1
                if len(self._inflight) > self._peak_inflight:
                    self._peak_inflight = len(self._inflight)
                should_compute = True

        if not should_compute:
            return future.result()  # type: ignore[no-any-return]

        try:
            result = factory()
            if self.cache is not None:
                with self._lock:
                    self.cache[key] = result
            future.set_result(result)
        except Exception as e:
            future.set_exception(e)
            raise
        finally:
            with self._lock:
                self._inflight.pop(key, None)
        return result

    def contains(self, key: Hashable) -> bool:
        """Peek whether ``key`` is cached, without triggering a compute."""
        return self.cache is not None and key in self.cache

    def stats(self) -> dict:
        """Snapshot of current/peak in-flight count, total computes started, and live keys.

        ``inflight_keys`` is copied under the lock so callers can iterate without
        racing on insertions or removals. Counters reset only on process restart.
        """
        with self._lock:
            return {
                "inflight": len(self._inflight),
                "inflight_keys": list(self._inflight.keys()),
                "peak_inflight": self._peak_inflight,
                "total_computes": self._total_computes,
            }

    def evict_matching(self, predicate: Callable[[Hashable], bool]) -> int:
        """Remove all cache entries whose key satisfies ``predicate``. Returns count removed."""
        if self.cache is None:
            return 0
        with self._lock:
            keys = [k for k in self.cache if predicate(k)]
            for k in keys:
                del self.cache[k]
        return len(keys)


class NullMemoizer(CacheBackend):
    """No caching, no dedup — every call runs ``factory()``. Explicit opt-out
    backend for ``CACHE_BACKEND=none``; a stampede of concurrent identical
    requests will all recompute, which is the accepted cost of disabling
    caching entirely."""

    def get_or_compute(self, key: Hashable, factory: Callable[[], T]) -> T:
        return factory()

    def contains(self, key: Hashable) -> bool:
        return False


class RedisMemoizer(CacheBackend):
    """Redis-backed cache + cross-instance single-flight dedup.

    Same ``get_or_compute`` contract as ``Memoizer``, but the cache and the
    in-flight coordination both live in Redis so every ECS instance shares
    them. A ``threading.Future`` only coordinates callers within one process,
    so cross-instance single-flight needs a real distributed lock plus a
    pub/sub wakeup so waiters aren't left with no way to find out when the
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

    def contains(self, key: Hashable) -> bool:
        return bool(self.redis.exists(self._cache_key(key)))

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
